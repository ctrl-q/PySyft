import torch as th

# The nn classes calls functional methods. Hence overloading functional methods allows
# to add custom functionality which also works with nn.layers.


def dropout(input, p=0.5, training=True, inplace=False):
    """
    Args:
        p: probability of an element to be zeroed. Default: 0.5
        training: If training, cause dropout layers are not used during evaluation of model
        inplace: If set to True, will do this operation in-place. Default: False
    """

    if training:
        binomial = th.distributions.binomial.Binomial(probs=1 - p)

        # we must convert the normal tensor to fixed precision before multiplication
        # Note that: Weights of a model are alwasy Float values
        # Hence input will always be of type (Wrapper) > FixedPrecisionTensor > ...
        noise = (binomial.sample(input.shape).type(th.FloatTensor) * (1.0 / (1.0 - p))).fix_prec(
            **input.get_class_attributes()
        )

        if inplace:
            input = input * noise
            return input

        return input * noise

    return input


def conv2d(input, weight, bias=None, stride=1, padding=0, dilation=1, groups=1):
    """
    Overloads torch.nn.functional.conv2d to be able to use MPC on convolutional networks.
    The idea is to build new tensors from input and weight to compute a
    matrix multiplication equivalent to the convolution.
    Args:
        input: input image
        weight: convolution kernels
        bias: optional additive bias
        stride: stride of the convolution kernels
        padding:  implicit paddings on both sides of the input.
        dilation: spacing between kernel elements
        groups: split input into groups, in_channels should be divisible by the number of groups
    Returns:
        the result of the convolution (FixedPrecision Tensor)
    """

    assert len(input.shape) == 4
    assert len(weight.shape) == 4

    # Change to tuple if not one
    stride = th.nn.modules.utils._pair(stride)
    padding = th.nn.modules.utils._pair(padding)
    dilation = th.nn.modules.utils._pair(dilation)

    # Extract a few useful values
    batch_size, nb_channels_in, nb_rows_in, nb_cols_in = input.shape
    nb_channels_out, nb_channels_kernel, nb_rows_kernel, nb_cols_kernel = weight.shape

    if bias is not None:
        assert len(bias) == nb_channels_out

    # Check if inputs are coherent
    assert nb_channels_in == nb_channels_kernel * groups
    assert nb_channels_in % groups == 0
    assert nb_channels_out % groups == 0

    # Compute output shape
    nb_rows_out = int(
        ((nb_rows_in + 2 * padding[0] - dilation[0] * (nb_rows_kernel - 1) - 1) / stride[0]) + 1
    )
    nb_cols_out = int(
        ((nb_cols_in + 2 * padding[1] - dilation[1] * (nb_cols_kernel - 1) - 1) / stride[1]) + 1
    )

    # Apply padding to the input
    if padding != (0, 0):
        padding_mode = "constant"
        input = th.nn.functional.pad(
            input, (padding[1], padding[1], padding[0], padding[0]), padding_mode
        )
        # Update shape after padding
        nb_rows_in += 2 * padding[0]
        nb_cols_in += 2 * padding[1]

    # We want to get relative positions of values in the input tensor that are used by one filter convolution.
    # It basically is the position of the values used for the top left convolution.
    pattern_ind = []
    for ch in range(nb_channels_in):
        for r in range(nb_rows_kernel):
            for c in range(nb_cols_kernel):
                pixel = r * nb_cols_in * dilation[0] + c * dilation[1]
                pattern_ind.append(pixel + ch * nb_rows_in * nb_cols_in)

    # The image tensor is reshaped for the matrix multiplication:
    # on each row of the new tensor will be the input values used for each filter convolution
    # We will get a matrix [[in values to compute out value 0],
    #                       [in values to compute out value 1],
    #                       ...
    #                       [in values to compute out value nb_rows_out*nb_cols_out]]
    im_flat = input.view(batch_size, -1)
    im_reshaped = []
    for cur_row_out in range(nb_rows_out):
        for cur_col_out in range(nb_cols_out):
            # For each new output value, we just need to shift the receptive field
            offset = cur_row_out * stride[0] * nb_cols_in + cur_col_out * stride[1]
            tmp = [ind + offset for ind in pattern_ind]
            im_reshaped.append(im_flat[:, tmp])
    im_reshaped = th.stack(im_reshaped).permute(1, 0, 2)

    # The convolution kernels are also reshaped for the matrix multiplication
    # We will get a matrix [[weights for out channel 0],
    #                       [weights for out channel 1],
    #                       ...
    #                       [weights for out channel nb_channels_out]].TRANSPOSE()
    weight_reshaped = weight.view(nb_channels_out // groups, -1).t()

    # Now that everything is set up, we can compute the result
    if groups > 1:
        res = []
        chunks_im = th.chunk(im_reshaped, groups, dim=2)
        chunks_weights = th.chunk(weight_reshaped, groups, dim=0)
        for g in range(groups):
            tmp = chunks_im[g].matmul(chunks_weights[g])
            res.append(tmp)
        res = th.cat(res, dim=2)
    else:
        res = im_reshaped.matmul(weight_reshaped)

    # Add a bias if needed
    if bias is not None:
        res += bias

    # ... And reshape it back to an image
    res = (
        res.permute(0, 2, 1)
        .view(batch_size, nb_channels_out, nb_rows_out, nb_cols_out)
        .contiguous()
    )
    return res
