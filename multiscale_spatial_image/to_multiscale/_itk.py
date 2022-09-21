from spatial_image import to_spatial_image
from dask.array import map_blocks, map_overlap
import numpy as np

from ._support import _align_chunks, _dim_scale_factors, _compute_sigma

def _get_block(current_input, block_index:int):
    '''Helper method for accessing an enumerated chunk from xarray input'''
    block_shape = [c[block_index] for c in current_input.chunks]
    block = current_input[tuple([slice(0, s) for s in block_shape])]
    # For consistency for now, do not utilize direction until there is standardized support for
    # direction cosines / orientation in OME-NGFF
    block.attrs.pop("direction", None)
    return block
        
def _compute_itk_gaussian_kernel_radius(input_size, sigma_values, shrink_factors) -> list:
    '''Get kernel radius in xyzt directions'''
    DEFAULT_MAX_KERNEL_WIDTH = 32
    MAX_KERNEL_ERROR = 0.01
    image_dimension = len(input_size)

    import itk

    # Constrain kernel width to be at most the size of one chunk
    max_kernel_width = min(DEFAULT_MAX_KERNEL_WIDTH, *input_size)
    variance = [sigma ** 2 for sigma in sigma_values]

    def generate_radius(direction:int) -> int:
        '''Follow itk.DiscreteGaussianImageFilter procedure to generate directional kernels'''
        oper = itk.GaussianOperator[itk.F, image_dimension]()
        oper.SetDirection(direction)
        oper.SetMaximumError(MAX_KERNEL_ERROR)
        oper.SetMaximumKernelWidth(max_kernel_width)
        oper.SetVariance(variance[direction])
        oper.CreateDirectional()
        return oper.GetRadius(direction)

    return [generate_radius(dim) for dim in range(image_dimension)]


def _itk_blur_and_downsample(xarray_data, gaussian_filter_name, interpolator_name, shrink_factors, sigma_values, kernel_radius):
    '''Blur and then downsample a given image chunk'''
    import itk
    
    # xarray chunk does not have metadata attached, values are ITK defaults
    image = itk.image_view_from_array(xarray_data)
    input_origin = itk.origin(image)

    # Skip this image block if it has 0 voxels
    block_size = itk.size(image)
    if(any([block_len == 0 for block_len in block_size])):
        return None
    
    # Output values are relative to input
    itk_shrink_factors = shrink_factors  # xyzt
    itk_kernel_radius = kernel_radius
    output_origin = [val + radius for val, radius in zip(input_origin, itk_kernel_radius)]
    output_spacing = [s * f for s, f in zip(itk.spacing(image), itk_shrink_factors)]
    output_size = [max(0,int((image_len - 2 * radius) / shrink_factor))
        for image_len, radius, shrink_factor in zip(itk.size(image), itk_kernel_radius, itk_shrink_factors)]

    # Optionally run accelerated smoothing with itk-vkfft
    if gaussian_filter_name == 'VkDiscreteGaussianImageFilter':
        smoothing_filter_template = itk.VkDiscreteGaussianImageFilter
    elif gaussian_filter_name == 'DiscreteGaussianImageFilter':
        smoothing_filter_template = itk.DiscreteGaussianImageFilter
    else:
        raise ValueError(f'Unsupported gaussian_filter {gaussian_filter_name}')

    # Construct pipeline
    smoothing_filter = smoothing_filter_template.New(image, 
        sigma_array=sigma_values, 
        use_image_spacing=False)            

    if interpolator_name == 'LinearInterpolateImageFunction':
        interpolator_instance = itk.LinearInterpolateImageFunction.New(smoothing_filter.GetOutput())
    elif interpolator_name == 'LabelImageGenericInterpolateImageFunction':
        interpolator_instance = itk.LabelImageGenericInterpolateImageFunction.New(smoothing_filter.GetOutput())
        # Similar approach as compute_sigma
        # Ref: https://link.springer.com/content/pdf/10.1007/978-3-319-24571-3_81.pdf
        sigma = [s * 0.7355 for s in output_spacing]
        sigma_max = max(sigma)
        interpolator_instance.SetSigma(sigma)
        interpolator_instance.SetAlpha(sigma_max * 2.5)
    else:
        raise ValueError(f'Unsupported interpolator_name {interpolator_name}')
    
    shrink_filter = itk.ResampleImageFilter.New(smoothing_filter.GetOutput(),
        interpolator=interpolator_instance,
        size=output_size,
        output_spacing=output_spacing,
        output_origin=output_origin)
    shrink_filter.Update()
    
    return shrink_filter.GetOutput()


def _downsample_itk_bin_shrink(current_input, default_chunks, out_chunks, scale_factors, data_objects, image):
    import itk

    for factor_index, scale_factor in enumerate(scale_factors):
        dim_factors = _dim_scale_factors(image.dims, scale_factor)
        current_input = _align_chunks(current_input, default_chunks, dim_factors)

        image_dims: Tuple[str, str, str, str] = ("x", "y", "z", "t")
        shrink_factors = [dim_factors[sf] for sf in image_dims if sf in dim_factors]

        block_0_shape = [c[0] for c in current_input.chunks]
        block_0 = current_input[tuple([slice(0, s) for s in block_0_shape])]
        # For consistency for now, do not utilize direction until there is standardized support for
        # direction cosines / orientation in OME-NGFF
        block_0.attrs.pop("direction", None)
        block_input = itk.image_from_xarray(block_0)
        filt = itk.BinShrinkImageFilter.New(
            block_input, shrink_factors=shrink_factors
        )
        filt.UpdateOutputInformation()
        block_output = filt.GetOutput()
        scale = {
            image_dims[i]: s for (i, s) in enumerate(block_output.GetSpacing())
        }
        translation = {
            image_dims[i]: s for (i, s) in enumerate(block_output.GetOrigin())
        }
        dtype = block_output.dtype
        output_chunks = list(current_input.chunks)
        for i, c in enumerate(output_chunks):
            output_chunks[i] = [
                block_output.shape[i],
            ] * len(c)

        block_neg1_shape = [c[-1] for c in current_input.chunks]
        block_neg1 = current_input[tuple([slice(0, s) for s in block_neg1_shape])]
        block_neg1.attrs.pop("direction", None)
        block_input = itk.image_from_xarray(block_neg1)
        filt = itk.BinShrinkImageFilter.New(
            block_input, shrink_factors=shrink_factors
        )
        filt.UpdateOutputInformation()
        block_output = filt.GetOutput()
        for i, c in enumerate(output_chunks):
            output_chunks[i][-1] = block_output.shape[i]
            output_chunks[i] = tuple(output_chunks[i])
        output_chunks = tuple(output_chunks)

        downscaled_array = map_blocks(
            itk.bin_shrink_image_filter,
            current_input.data,
            shrink_factors=shrink_factors,
            dtype=dtype,
            chunks=output_chunks,
        )
        downscaled = to_spatial_image(
            downscaled_array,
            dims=image.dims,
            scale=scale,
            translation=translation,
            name=current_input.name,
            axis_names={
                d: image.coords[d].attrs.get("long_name", d) for d in image.dims
            },
            axis_units={
                d: image.coords[d].attrs.get("units", "") for d in image.dims
            },
            t_coords=image.coords.get("t", None),
            c_coords=image.coords.get("c", None),
        )
        downscaled = downscaled.chunk(out_chunks)
        data_objects[f"scale{factor_index+1}"] = downscaled.to_dataset(
            name=image.name, promote_attrs=True
        )
        current_input = downscaled

    return data_objects


def _downsample_itk_gaussian(current_input, default_chunks, out_chunks, scale_factors, data_objects, image):
    import itk         

    # Optionally run accelerated smoothing with itk-vkfft
    if 'VkFFTBackend' in dir(itk):
        gaussian_filter_name = 'VkDiscreteGaussianImageFilter'
    else:
        gaussian_filter_name = 'DiscreteGaussianImageFilter'

    interpolator_name = 'LinearInterpolateImageFunction'

    for factor_index, scale_factor in enumerate(scale_factors):
        dim_factors = _dim_scale_factors(image.dims, scale_factor)
        current_input = _align_chunks(current_input, default_chunks, dim_factors)

        image_dims: Tuple[str, str, str, str] = ("x", "y", "z", "t")
        shrink_factors = [dim_factors[sf] for sf in image_dims if sf in dim_factors]

        # Compute metadata for region splitting

        # Blocks 0, ..., N-2 have the same shape
        block_0_input = _get_block(current_input,0)
        # Block N-1 may be smaller than preceding blocks
        block_neg1_input = _get_block(current_input,-1)

        # Compute overlap for Gaussian blurring for all blocks
        block_0_image = itk.image_from_xarray(block_0_input)
        input_spacing = itk.spacing(block_0_image)
        sigma_values = _compute_sigma(input_spacing, shrink_factors)
        kernel_radius = _compute_itk_gaussian_kernel_radius(itk.size(block_0_image), sigma_values, shrink_factors)

        # Compute output size and spatial metadata for blocks 0, .., N-2
        filt = itk.BinShrinkImageFilter.New(
            block_0_image, shrink_factors=shrink_factors
        )
        filt.UpdateOutputInformation()
        block_output = filt.GetOutput()
        block_0_output_spacing = block_output.GetSpacing()
        block_0_output_origin = block_output.GetOrigin()
        
        block_0_scale = {
            image_dims[i]: s for (i, s) in enumerate(block_0_output_spacing)
        }
        block_0_translation = {
            image_dims[i]: s for (i, s) in enumerate(block_0_output_origin)
        }
        dtype = block_output.dtype
        
        computed_size = [int(block_len / shrink_factor) 
            for block_len, shrink_factor in zip(itk.size(block_0_image), shrink_factors)]
        assert all([itk.size(block_output)[dim] == computed_size[dim]
                    for dim in range(block_output.ndim)])
        output_chunks = list(current_input.chunks)
        for i, c in enumerate(output_chunks):
            output_chunks[i] = [
                block_output.shape[i],
            ] * len(c)

        # Compute output size for block N-1
        block_neg1_image = itk.image_from_xarray(block_neg1_input)
        filt.SetInput(block_neg1_image)
        filt.UpdateOutputInformation()
        block_output = filt.GetOutput()
        computed_size = [int(block_len / shrink_factor) 
            for block_len, shrink_factor in zip(itk.size(block_neg1_image), shrink_factors)]
        assert all([itk.size(block_output)[dim] == computed_size[dim]
                    for dim in range(block_output.ndim)])
        for i, c in enumerate(output_chunks):
            output_chunks[i][-1] = block_output.shape[i]
            output_chunks[i] = tuple(output_chunks[i])
        output_chunks = tuple(output_chunks)

        downscaled_array = map_overlap(
          _itk_blur_and_downsample,
          current_input.data,
          gaussian_filter_name=gaussian_filter_name,
          interpolator_name=interpolator_name,
          shrink_factors=shrink_factors,
          sigma_values=sigma_values,
          kernel_radius=kernel_radius,
          dtype=dtype,
          depth={dim: radius for dim, radius in enumerate(np.flip(kernel_radius))}, # overlap is in tzyx
          boundary='nearest',
          trim=False   # Overlapped region is trimmed in blur_and_downsample to output size
        ).compute()
        
        downscaled = to_spatial_image(
            downscaled_array,
            dims=image.dims,
            scale=block_0_scale,
            translation=block_0_translation,
            name=current_input.name,
            axis_names={
                d: image.coords[d].attrs.get("long_name", d) for d in image.dims
            },
            axis_units={
                d: image.coords[d].attrs.get("units", "") for d in image.dims
            },
            t_coords=image.coords.get("t", None),
            c_coords=image.coords.get("c", None),
        )
        downscaled = downscaled.chunk(out_chunks)
        data_objects[f"scale{factor_index+1}"] = downscaled.to_dataset(
            name=image.name, promote_attrs=True
        )
        current_input = downscaled
    return data_objects

def _downsample_itk_label(current_input, default_chunks, out_chunks, scale_factors, data_objects, image):
    # Uses the LabelImageGenericInterpolateImageFunction. More appropriate for integer label images.
    import itk         

    gaussian_filter_name = 'DiscreteGaussianImageFilter'
    interpolator_name = 'LabelImageGenericInterpolateImageFunction'

    for factor_index, scale_factor in enumerate(scale_factors):
        dim_factors = _dim_scale_factors(image.dims, scale_factor)
        current_input = _align_chunks(current_input, default_chunks, dim_factors)

        image_dims: Tuple[str, str, str, str] = ("x", "y", "z", "t")
        shrink_factors = [dim_factors[sf] for sf in image_dims if sf in dim_factors]

        # Compute metadata for region splitting

        # Blocks 0, ..., N-2 have the same shape
        block_0_input = _get_block(current_input,0)
        # Block N-1 may be smaller than preceding blocks
        block_neg1_input = _get_block(current_input,-1)

        # Compute overlap for Gaussian blurring for all blocks
        block_0_image = itk.image_from_xarray(block_0_input)
        input_spacing = itk.spacing(block_0_image)
        sigma_values = _compute_sigma(input_spacing, shrink_factors)
        kernel_radius = _compute_itk_gaussian_kernel_radius(itk.size(block_0_image), sigma_values, shrink_factors)

        # Compute output size and spatial metadata for blocks 0, .., N-2
        filt = itk.BinShrinkImageFilter.New(
            block_0_image, shrink_factors=shrink_factors
        )
        filt.UpdateOutputInformation()
        block_output = filt.GetOutput()
        block_0_output_spacing = block_output.GetSpacing()
        block_0_output_origin = block_output.GetOrigin()
        
        block_0_scale = {
            image_dims[i]: s for (i, s) in enumerate(block_0_output_spacing)
        }
        block_0_translation = {
            image_dims[i]: s for (i, s) in enumerate(block_0_output_origin)
        }
        dtype = block_output.dtype
        
        computed_size = [int(block_len / shrink_factor) 
            for block_len, shrink_factor in zip(itk.size(block_0_image), shrink_factors)]
        assert all([itk.size(block_output)[dim] == computed_size[dim]
                    for dim in range(block_output.ndim)])
        output_chunks = list(current_input.chunks)
        for i, c in enumerate(output_chunks):
            output_chunks[i] = [
                block_output.shape[i],
            ] * len(c)

        # Compute output size for block N-1
        block_neg1_image = itk.image_from_xarray(block_neg1_input)
        filt.SetInput(block_neg1_image)
        filt.UpdateOutputInformation()
        block_output = filt.GetOutput()
        computed_size = [int(block_len / shrink_factor) 
            for block_len, shrink_factor in zip(itk.size(block_neg1_image), shrink_factors)]
        assert all([itk.size(block_output)[dim] == computed_size[dim]
                    for dim in range(block_output.ndim)])
        for i, c in enumerate(output_chunks):
            output_chunks[i][-1] = block_output.shape[i]
            output_chunks[i] = tuple(output_chunks[i])
        output_chunks = tuple(output_chunks)

        downscaled_array = map_overlap(
          _itk_blur_and_downsample,
          current_input.data,
          gaussian_filter_name=gaussian_filter_name,
          interpolator_name=interpolator_name,
          shrink_factors=shrink_factors,
          sigma_values=sigma_values,
          kernel_radius=kernel_radius,
          dtype=dtype,
          depth={dim: radius for dim, radius in enumerate(np.flip(kernel_radius))}, # overlap is in tzyx
          boundary='nearest',
          trim=False   # Overlapped region is trimmed in blur_and_downsample to output size
        ).compute()
        
        downscaled = to_spatial_image(
            downscaled_array,
            dims=image.dims,
            scale=block_0_scale,
            translation=block_0_translation,
            name=current_input.name,
            axis_names={
                d: image.coords[d].attrs.get("long_name", d) for d in image.dims
            },
            axis_units={
                d: image.coords[d].attrs.get("units", "") for d in image.dims
            },
            t_coords=image.coords.get("t", None),
            c_coords=image.coords.get("c", None),
        )
        downscaled = downscaled.chunk(out_chunks)
        data_objects[f"scale{factor_index+1}"] = downscaled.to_dataset(
            name=image.name, promote_attrs=True
        )
        current_input = downscaled

    return data_objects