import logging
import os

import diffusers
import torch
from diffusers import FluxTransformer2DModel
from diffusers.configuration_utils import register_to_config
from huggingface_hub import utils
from packaging.version import Version
from safetensors.torch import load_file
from torch import nn

from .utils import get_precision, NunchakuModelLoaderMixin, pad_tensor
from ..._C import QuantizedFluxModel, utils as cutils
from ...lora.flux.nunchaku_converter import fuse_vectors, to_nunchaku
from ...lora.flux.utils import is_nunchaku_format
from ...utils import load_state_dict_in_safetensors

SVD_RANK = 32

# Get log level from environment variable (default to INFO)
log_level = os.getenv("LOG_LEVEL", "INFO").upper()

# Configure logging
logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class NunchakuFluxTransformerBlocks(nn.Module):
    def __init__(self, m: QuantizedFluxModel, device: str | torch.device):
        super(NunchakuFluxTransformerBlocks, self).__init__()
        self.m = m
        self.dtype = torch.bfloat16
        self.device = device

    @staticmethod
    def pack_rotemb(rotemb: torch.Tensor) -> torch.Tensor:
        assert rotemb.dtype == torch.float32
        B = rotemb.shape[0]
        M = rotemb.shape[1]
        D = rotemb.shape[2] * 2
        assert rotemb.shape == (B, M, D // 2, 1, 2)
        assert M % 16 == 0
        assert D % 8 == 0
        rotemb = rotemb.reshape(B, M // 16, 16, D // 8, 8)
        rotemb = rotemb.permute(0, 1, 3, 2, 4)
        # 16*8 pack, FP32 accumulator (C) format
        # https://docs.nvidia.com/cuda/parallel-thread-execution/#mma-16816-c
        ##########################################|--M--|--D--|
        ##########################################|-3--4--5--6|
        ##########################################  :  :  :  :
        rotemb = rotemb.reshape(*rotemb.shape[0:3], 2, 8, 4, 2)
        rotemb = rotemb.permute(0, 1, 2, 4, 5, 3, 6)
        rotemb = rotemb.contiguous()
        rotemb = rotemb.view(B, M, D)
        return rotemb

    def forward(
        self,
        hidden_states: torch.Tensor,
        temb: torch.Tensor,
        encoder_hidden_states: torch.Tensor,
        image_rotary_emb: torch.Tensor,
        joint_attention_kwargs=None,
        skip_first_layer=False,
    ):
        batch_size = hidden_states.shape[0]
        txt_tokens = encoder_hidden_states.shape[1]
        img_tokens = hidden_states.shape[1]

        original_dtype = hidden_states.dtype
        original_device = hidden_states.device

        hidden_states = hidden_states.to(self.dtype).to(self.device)
        encoder_hidden_states = encoder_hidden_states.to(self.dtype).to(self.device)
        temb = temb.to(self.dtype).to(self.device)
        image_rotary_emb = image_rotary_emb.to(self.device)

        assert image_rotary_emb.ndim == 6
        assert image_rotary_emb.shape[0] == 1
        assert image_rotary_emb.shape[1] == 1
        assert image_rotary_emb.shape[2] == batch_size * (txt_tokens + img_tokens)
        # [bs, tokens, head_dim / 2, 1, 2] (sincos)
        image_rotary_emb = image_rotary_emb.reshape([batch_size, txt_tokens + img_tokens, *image_rotary_emb.shape[3:]])
        rotary_emb_txt = image_rotary_emb[:, :txt_tokens, ...]  # .to(self.dtype)
        rotary_emb_img = image_rotary_emb[:, txt_tokens:, ...]  # .to(self.dtype)
        rotary_emb_single = image_rotary_emb  # .to(self.dtype)

        rotary_emb_txt = self.pack_rotemb(pad_tensor(rotary_emb_txt, 256, 1))
        rotary_emb_img = self.pack_rotemb(pad_tensor(rotary_emb_img, 256, 1))
        rotary_emb_single = self.pack_rotemb(pad_tensor(rotary_emb_single, 256, 1))

        hidden_states = self.m.forward(
            hidden_states,
            encoder_hidden_states,
            temb,
            rotary_emb_img,
            rotary_emb_txt,
            rotary_emb_single,
            skip_first_layer,
        )

        hidden_states = hidden_states.to(original_dtype).to(original_device)

        encoder_hidden_states = hidden_states[:, :txt_tokens, ...]
        hidden_states = hidden_states[:, txt_tokens:, ...]

        return encoder_hidden_states, hidden_states

    def forward_layer_at(
        self,
        idx: int,
        hidden_states: torch.Tensor,
        encoder_hidden_states: torch.Tensor,
        temb: torch.Tensor,
        image_rotary_emb: torch.Tensor,
        joint_attention_kwargs=None,
    ):
        batch_size = hidden_states.shape[0]
        txt_tokens = encoder_hidden_states.shape[1]
        img_tokens = hidden_states.shape[1]

        original_dtype = hidden_states.dtype
        original_device = hidden_states.device

        hidden_states = hidden_states.to(self.dtype).to(self.device)
        encoder_hidden_states = encoder_hidden_states.to(self.dtype).to(self.device)
        temb = temb.to(self.dtype).to(self.device)
        image_rotary_emb = image_rotary_emb.to(self.device)

        assert image_rotary_emb.ndim == 6
        assert image_rotary_emb.shape[0] == 1
        assert image_rotary_emb.shape[1] == 1
        assert image_rotary_emb.shape[2] == batch_size * (txt_tokens + img_tokens)
        # [bs, tokens, head_dim / 2, 1, 2] (sincos)
        image_rotary_emb = image_rotary_emb.reshape([batch_size, txt_tokens + img_tokens, *image_rotary_emb.shape[3:]])
        rotary_emb_txt = image_rotary_emb[:, :txt_tokens, ...]  # .to(self.dtype)
        rotary_emb_img = image_rotary_emb[:, txt_tokens:, ...]  # .to(self.dtype)

        rotary_emb_txt = self.pack_rotemb(pad_tensor(rotary_emb_txt, 256, 1))
        rotary_emb_img = self.pack_rotemb(pad_tensor(rotary_emb_img, 256, 1))

        hidden_states, encoder_hidden_states = self.m.forward_layer(
            idx, hidden_states, encoder_hidden_states, temb, rotary_emb_img, rotary_emb_txt
        )

        hidden_states = hidden_states.to(original_dtype).to(original_device)
        encoder_hidden_states = encoder_hidden_states.to(original_dtype).to(original_device)

        return encoder_hidden_states, hidden_states


## copied from diffusers 0.30.3
def rope(pos: torch.Tensor, dim: int, theta: int) -> torch.Tensor:
    assert dim % 2 == 0, "The dimension must be even."

    scale = torch.arange(0, dim, 2, dtype=torch.float64, device=pos.device) / dim
    omega = 1.0 / (theta**scale)

    batch_size, seq_length = pos.shape
    out = torch.einsum("...n,d->...nd", pos, omega)

    USE_SINCOS = True
    if USE_SINCOS:
        cos_out = torch.cos(out)
        sin_out = torch.sin(out)
        stacked_out = torch.stack([sin_out, cos_out], dim=-1)
        out = stacked_out.view(batch_size, -1, dim // 2, 1, 2)
    else:
        out = out.view(batch_size, -1, dim // 2, 1, 1)

    return out.float()


class EmbedND(nn.Module):
    def __init__(self, dim: int, theta: int, axes_dim: list[int]):
        super(EmbedND, self).__init__()
        self.dim = dim
        self.theta = theta
        self.axes_dim = axes_dim

    def forward(self, ids: torch.Tensor) -> torch.Tensor:
        if Version(diffusers.__version__) >= Version("0.31.0"):
            ids = ids[None, ...]
        n_axes = ids.shape[-1]
        emb = torch.cat([rope(ids[..., i], self.axes_dim[i], self.theta) for i in range(n_axes)], dim=-3)
        return emb.unsqueeze(1)


def load_quantized_module(
    path: str, device: str | torch.device = "cuda", use_fp4: bool = False, offload: bool = False
) -> QuantizedFluxModel:
    device = torch.device(device)
    assert device.type == "cuda"
    m = QuantizedFluxModel()
    cutils.disable_memory_auto_release()
    m.init(use_fp4, offload, True, 0 if device.index is None else device.index)
    m.load(path)
    return m


class NunchakuFluxTransformer2dModel(FluxTransformer2DModel, NunchakuModelLoaderMixin):
    @register_to_config
    def __init__(
        self,
        patch_size: int = 1,
        in_channels: int = 64,
        out_channels: int | None = None,
        num_layers: int = 19,
        num_single_layers: int = 38,
        attention_head_dim: int = 128,
        num_attention_heads: int = 24,
        joint_attention_dim: int = 4096,
        pooled_projection_dim: int = 768,
        guidance_embeds: bool = False,
        axes_dims_rope: tuple[int] = (16, 56, 56),
    ):
        super(NunchakuFluxTransformer2dModel, self).__init__(
            patch_size=patch_size,
            in_channels=in_channels,
            out_channels=out_channels,
            num_layers=num_layers,
            num_single_layers=num_single_layers,
            attention_head_dim=attention_head_dim,
            num_attention_heads=num_attention_heads,
            joint_attention_dim=joint_attention_dim,
            pooled_projection_dim=pooled_projection_dim,
            guidance_embeds=guidance_embeds,
            axes_dims_rope=axes_dims_rope,
        )
        # these state_dicts are used for supporting lora
        self._unquantized_part_sd: dict[str, torch.Tensor] = {}
        self._unquantized_part_loras: dict[str, torch.Tensor] = {}
        self._quantized_part_sd: dict[str, torch.Tensor] = {}
        self._quantized_part_vectors: dict[str, torch.Tensor] = {}

    @classmethod
    @utils.validate_hf_hub_args
    def from_pretrained(cls, pretrained_model_name_or_path: str | os.PathLike, **kwargs):
        device = kwargs.get("device", "cuda")
        if isinstance(device, str):
            device = torch.device(device)
        offload = kwargs.get("offload", False)
        precision = get_precision(kwargs.get("precision", "auto"), device, pretrained_model_name_or_path)
        transformer, unquantized_part_path, transformer_block_path = cls._build_model(
            pretrained_model_name_or_path, **kwargs
        )

        # get the default LoRA branch and all the vectors
        quantized_part_sd = load_file(transformer_block_path)
        new_quantized_part_sd = {}
        for k, v in quantized_part_sd.items():
            if v.ndim == 1:
                new_quantized_part_sd[k] = v
            elif "qweight" in k:
                # only the shape information of this tensor is needed
                new_quantized_part_sd[k] = v.to("meta")
            elif "lora" in k:
                new_quantized_part_sd[k] = v
        transformer._quantized_part_sd = new_quantized_part_sd
        m = load_quantized_module(transformer_block_path, device=device, use_fp4=precision == "fp4", offload=offload)
        transformer.inject_quantized_module(m, device)
        transformer.to_empty(device=device)

        unquantized_part_sd = load_file(unquantized_part_path)
        transformer.load_state_dict(unquantized_part_sd, strict=False)
        transformer._unquantized_part_sd = unquantized_part_sd

        return transformer

    def inject_quantized_module(self, m: QuantizedFluxModel, device: str | torch.device = "cuda"):
        print("Injecting quantized module")
        self.pos_embed = EmbedND(dim=self.inner_dim, theta=10000, axes_dim=[16, 56, 56])

        ### Compatible with the original forward method
        self.transformer_blocks = nn.ModuleList([NunchakuFluxTransformerBlocks(m, device)])
        self.single_transformer_blocks = nn.ModuleList([])

        return self

    def set_attention_impl(self, impl: str):
        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxTransformerBlocks)
        block.m.setAttentionImpl(impl)

    ### LoRA Related Functions

    def _expand_module(self, module_name: str, new_shape: tuple[int, int]):
        module = self.get_submodule(module_name)
        assert isinstance(module, nn.Linear)
        weight_shape = module.weight.shape
        logger.info("Expand the shape of module {} from {} to {}".format(module_name, tuple(weight_shape), new_shape))
        assert new_shape[0] >= weight_shape[0] and new_shape[1] >= weight_shape[1]
        new_module = nn.Linear(
            new_shape[1],
            new_shape[0],
            bias=module.bias is not None,
            device=module.weight.device,
            dtype=module.weight.dtype,
        )
        new_module.weight.data.zero_()
        new_module.weight.data[: weight_shape[0], : weight_shape[1]] = module.weight.data
        self._unquantized_part_sd[f"{module_name}.weight"] = new_module.weight.data.clone()
        if new_module.bias is not None:
            new_module.bias.data.zero_()
            new_module.bias.data[: weight_shape[0]] = module.bias.data
            self._unquantized_part_sd[f"{module_name}.bias"] = new_module.bias.data.clone()
        parent_name = ".".join(module_name.split(".")[:-1])
        parent_module = self.get_submodule(parent_name)
        parent_module.add_module(module_name.split(".")[-1], new_module)

        if module_name == "x_embedder":
            new_value = int(new_module.weight.data.shape[1])
            old_value = getattr(self.config, "in_channels")
            if new_value != old_value:
                logger.info(f"Update in_channels from {old_value} to {new_value}")
                setattr(self.config, "in_channels", new_value)

    def _update_unquantized_part_lora_params(self, strength: float = 1):
        # check if we need to expand the linear layers
        device = next(self.parameters()).device
        for k, v in self._unquantized_part_loras.items():
            if "lora_A" in k:
                lora_a = v
                lora_b = self._unquantized_part_loras[k.replace(".lora_A.", ".lora_B.")]
                diff_shape = (lora_b.shape[0], lora_a.shape[1])
                weight_shape = self._unquantized_part_sd[k.replace(".lora_A.", ".")].shape
                if diff_shape[0] > weight_shape[0] or diff_shape[1] > weight_shape[1]:
                    module_name = ".".join(k.split(".")[:-2])
                    self._expand_module(module_name, diff_shape)
            elif v.ndim == 1:
                diff_shape = v.shape
                weight_shape = self._unquantized_part_sd[k].shape
                if diff_shape[0] > weight_shape[0]:
                    assert diff_shape[0] >= weight_shape[0]
                    module_name = ".".join(k.split(".")[:-1])
                    module = self.get_submodule(module_name)
                    weight_shape = module.weight.shape
                    diff_shape = (diff_shape[0], weight_shape[1])
                    self._expand_module(module_name, diff_shape)
        new_state_dict = {}
        for k in self._unquantized_part_sd.keys():
            v = self._unquantized_part_sd[k]
            v = v.to(device)
            self._unquantized_part_sd[k] = v

            if v.ndim == 1 and k in self._unquantized_part_loras:
                diff = strength * self._unquantized_part_loras[k]
                if diff.shape[0] < v.shape[0]:
                    diff = torch.cat(
                        [diff, torch.zeros(v.shape[0] - diff.shape[0], device=device, dtype=v.dtype)], dim=0
                    )
                new_state_dict[k] = v + diff
            elif v.ndim == 2 and k.replace(".weight", ".lora_B.weight") in self._unquantized_part_loras:
                lora_a = self._unquantized_part_loras[k.replace(".weight", ".lora_A.weight")]
                lora_b = self._unquantized_part_loras[k.replace(".weight", ".lora_B.weight")]

                if lora_a.shape[1] < v.shape[1]:
                    lora_a = torch.cat(
                        [
                            lora_a,
                            torch.zeros(lora_a.shape[0], v.shape[1] - lora_a.shape[1], device=device, dtype=v.dtype),
                        ],
                        dim=1,
                    )
                if lora_b.shape[0] < v.shape[0]:
                    lora_b = torch.cat(
                        [
                            lora_b,
                            torch.zeros(v.shape[0] - lora_b.shape[0], lora_b.shape[1], device=device, dtype=v.dtype),
                        ],
                        dim=0,
                    )

                diff = strength * (lora_b @ lora_a)
                new_state_dict[k] = v + diff
            else:
                new_state_dict[k] = v
        self.load_state_dict(new_state_dict, strict=True)

    def update_lora_params(self, path_or_state_dict: str | dict[str, torch.Tensor]):
        if isinstance(path_or_state_dict, dict):
            state_dict = {
                k: v for k, v in path_or_state_dict.items()
            }  # copy a new one to avoid modifying the original one
        else:
            state_dict = load_state_dict_in_safetensors(path_or_state_dict)

        if not is_nunchaku_format(state_dict):
            state_dict = to_nunchaku(state_dict, base_sd=self._quantized_part_sd)

        unquantized_part_loras = {}
        for k, v in list(state_dict.items()):
            device = next(self.parameters()).device
            if "transformer_blocks" not in k:
                unquantized_part_loras[k] = state_dict.pop(k).to(device)

        if len(self._unquantized_part_loras) > 0 or len(unquantized_part_loras) > 0:
            self._unquantized_part_loras = unquantized_part_loras
            self._update_unquantized_part_lora_params(1)

        quantized_part_vectors = {}
        for k, v in list(state_dict.items()):
            if v.ndim == 1:
                quantized_part_vectors[k] = state_dict.pop(k)
        if len(self._quantized_part_vectors) > 0 or len(quantized_part_vectors) > 0:
            self._quantized_part_vectors = quantized_part_vectors
            updated_vectors = fuse_vectors(quantized_part_vectors, self._quantized_part_sd, 1)
            state_dict.update(updated_vectors)

        # Get the vectors from the quantized part

        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxTransformerBlocks)

        block.m.loadDict(state_dict, True)

    # This function can only be used with a single LoRA.
    # For multiple LoRAs, please fuse the lora scale into the weights.
    def set_lora_strength(self, strength: float = 1):
        block = self.transformer_blocks[0]
        assert isinstance(block, NunchakuFluxTransformerBlocks)
        block.m.setLoraScale(SVD_RANK, strength)
        if len(self._unquantized_part_loras) > 0:
            self._update_unquantized_part_lora_params(strength)
        if len(self._quantized_part_vectors) > 0:
            vector_dict = fuse_vectors(self._quantized_part_vectors, self._quantized_part_sd, strength)
            block.m.loadDict(vector_dict, True)
