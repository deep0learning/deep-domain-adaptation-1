from tensorflow.python.framework import constant_op
from tensorflow.python.framework import ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops


def lr_annealing(learning_rate, global_step, total_steps, alpha, beta, name=None):
    """
    Applies learning rate annealing to the initial learning rate
    return lr_p = learning_rate * (1 + alpha * (global_step / total_step)^(-beta)
    """
    with ops.name_scope(name, "Lr_Annealing", [learning_rate, global_step, total_steps, alpha, beta]) as name:
        learning_rate = ops.convert_to_tensor(learning_rate, name="learning_rate")
        dtype = learning_rate.dtype
        global_step = math_ops.cast(global_step, dtype)
        total_steps = math_ops.cast(total_steps, dtype)
        alpha = math_ops.cast(alpha, dtype)
        beta = math_ops.cast(beta, dtype)
        base = math_ops.divide(global_step, total_steps)
        base = math_ops.multiply(alpha, base)
        return math_ops.multiply(learning_rate, math_ops.pow(base, -beta), name=name)
