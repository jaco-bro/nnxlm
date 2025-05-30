import jax
import jax.numpy as jnp
from flax import nnx
from .utils import apply_rope

class Attention(nnx.Module):
    def __init__(self, config, *, rngs: nnx.Rngs):
        self.n_heads = n_heads = config.num_attention_heads
        self.n_kv_heads = n_kv_heads = config.num_key_value_heads
        self.head_dim = head_dim = config.hidden_size // n_heads
        self.scale = head_dim ** -0.5
        op_size = n_heads * head_dim + 2 * (n_kv_heads * head_dim)
        self.qkv_proj = nnx.Linear(in_features=config.hidden_size, out_features=op_size, use_bias=False, rngs=rngs)
        self.o_proj = nnx.Linear(in_features=n_heads * head_dim, out_features=config.hidden_size, use_bias=False, rngs=rngs)
        self.rot_dims=int(self.head_dim*config.partial_rotary_factor)

    @nnx.jit
    def __call__(self, x, attention_mask, rope, cache):
        B, L, _ = x.shape
        qkv = self.qkv_proj(x)
        query_pos = self.n_heads * self.head_dim
        kv_pos = query_pos + self.n_kv_heads * self.head_dim
        q = qkv[:, :, :query_pos]
        k = qkv[:, :, query_pos:kv_pos]
        v = qkv[:, :, kv_pos:]
        q = q.reshape(B, L, self.n_heads, self.head_dim)
        k = k.reshape(B, L, self.n_kv_heads, self.head_dim)
        v = v.reshape(B, L, self.n_kv_heads, self.head_dim)
        q = jnp.transpose(q, (0, 2, 1, 3)) 
        k = jnp.transpose(k, (0, 2, 1, 3))
        v = jnp.transpose(v, (0, 2, 1, 3))  
        q, k = apply_rope(q, k, *rope, self.rot_dims)
        if cache is not None:
            k, v = cache(k, v)
        if self.n_heads > self.n_kv_heads:
            n_repeat = self.n_heads // self.n_kv_heads
            k = k.repeat(repeats=n_repeat, axis=1)
            v = v.repeat(repeats=n_repeat, axis=1)
        w = jnp.matmul(q, k.swapaxes(-1, -2)) * self.scale
        w = w + attention_mask
        w = jax.nn.softmax(w, axis=-1)
        w = jnp.matmul(w, v)
        w = w.transpose((0, 2, 1, 3))
        w = w.reshape(B, L, -1)
        return self.o_proj(w)

class MLP(nnx.Module):
    def __init__(self, config, *, rngs: nnx.Rngs):
        self.gate_up_proj = nnx.Linear(in_features=config.hidden_size, out_features=2 * config.intermediate_size, use_bias=False, rngs=rngs)
        self.down_proj = nnx.Linear(in_features=config.intermediate_size, out_features=config.hidden_size, use_bias=False, rngs=rngs)
    
    @nnx.jit
    def __call__(self, x: jax.Array) -> jax.Array:
        x = self.gate_up_proj(x)
        gate, up = jnp.split(x, 2, axis=-1)
        return self.down_proj(jax.nn.silu(gate) * up)

class TransformerBlock(nnx.Module):
    def __init__(self, config, *, rngs: nnx.Rngs):
        self.self_attn = Attention(config, rngs=rngs)
        self.mlp = MLP(config, rngs=rngs)
        self.input_layernorm = nnx.RMSNorm(num_features=config.hidden_size, epsilon=config.rms_norm_eps, rngs=rngs)
        self.post_attention_layernorm = nnx.RMSNorm(num_features=config.hidden_size, epsilon=config.rms_norm_eps, rngs=rngs)
    
    @nnx.jit
    def __call__(self, x, attention_mask, rope, cache=None):
        h = self.self_attn(
            self.input_layernorm(x),
            attention_mask=attention_mask,
            rope=rope,
            cache=cache,
        )
        x = x + h
        return x + self.mlp(self.post_attention_layernorm(x))

class Phi3Model(nnx.Module):
    def __init__(self, config, *, rngs: nnx.Rngs):
        self.layers = [TransformerBlock(config, rngs=rngs) for _ in range(config.num_hidden_layers)]
        self.embed_tokens = nnx.Embed(num_embeddings=config.vocab_size, features=config.hidden_size, rngs=rngs)
        self.norm = nnx.RMSNorm(num_features=config.hidden_size, epsilon=config.rms_norm_eps, rngs=rngs)
    
    @nnx.jit
    def __call__(self, input_ids, attention_mask, rope, cache):
        x = self.embed_tokens(input_ids)
        for i, layer in enumerate(self.layers):
            x = layer(x, attention_mask=attention_mask, rope=rope, cache=cache[i])
        return self.norm(x)
    
class Phi3ForCausalLM(nnx.Module):
    def __init__(self, config, *, rngs: nnx.Rngs):
        self.tie = tie = config.tie_word_embeddings
        self.model = Phi3Model(config, rngs=rngs)
        if not tie:
            self.lm_head = nnx.Linear(in_features=config.hidden_size, out_features=config.vocab_size, use_bias=False, rngs=rngs)
    
    @nnx.jit
    def __call__(self, input_ids, attention_mask, rope, cache):
        x = self.model(input_ids, attention_mask=attention_mask, rope=rope, cache=cache)
        if self.tie:
            x = self.model.embed_tokens.attend(x)
        else:
            x = self.lm_head(x)
        return x
