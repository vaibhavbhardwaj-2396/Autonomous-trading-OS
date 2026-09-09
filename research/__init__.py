"""Living Quant research layer.

One-way dependency: research/ may import engine/; engine/ must NEVER import
research/. Enforced by tests/test_kernel_isolation.py.
"""
