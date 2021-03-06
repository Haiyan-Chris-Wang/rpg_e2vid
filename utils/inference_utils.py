from .util import robust_min, robust_max
from .path_utils import ensure_dir
from .timers import Timer, CudaTimer
from .loading_utils import get_device
from os.path import join
from math import ceil, floor
from torch.nn import ZeroPad2d
import numpy as np
import torch
import cv2
from collections import deque
import atexit


def make_event_preview(events, color=False):
    event_preview = events[0, :, :, :].detach().cpu().numpy()
    event_preview = np.sum(event_preview, axis=0)

    # normalize event image to [0, 255] for display
    m, M = -10.0, 10.0
    event_preview = np.clip((255.0 * (event_preview - m) / (M - m)).astype(np.uint8), 0, 255)

    if color:
        event_preview = np.dstack([event_preview] * 3)

    return event_preview


class EventPreprocessor:
    """
    Utility class to preprocess event tensors.
    Can perform operations such as hot pixel removing, event tensor normalization,
    or flipping the event tensor.
    """

    def __init__(self, options):

        print('== Event preprocessing ==')
        self.no_normalize = options.no_normalize
        if self.no_normalize:
            print('!!Will not normalize event tensors!!')
        else:
            print('Will normalize event tensors.')

        self.hot_pixel_locations = []
        if options.hot_pixels_file:
            try:
                self.hot_pixel_locations = np.loadtxt(options.hot_pixels_file, delimiter=',').astype(np.int)
                print('Will remove {} hot pixels'.format(self.hot_pixel_locations.shape[0]))
            except IOError:
                print('WARNING: could not load hot pixels file: {}'.format(options.hot_pixels_file))

        self.flip = options.flip
        if self.flip:
            print('Will flip event tensors.')

    def __call__(self, events):

        # Remove (i.e. zero out) the hot pixels
        for x, y in self.hot_pixel_locations:
            events[:, :, y, x] = 0

        # Flip tensor vertically and horizontally
        if self.flip:
            events = torch.flip(events, dims=[2, 3])

        # Normalize the event tensor (voxel grid) so that
        # the mean and stddev of the nonzero values in the tensor are equal to (0.0, 1.0)
        if not self.no_normalize:
            with CudaTimer('Normalization'):
                mean, stddev = events[events != 0].mean(), events[events != 0].std()
                events[events != 0] = (events[events != 0] - mean) / stddev

        return events


class IntensityRescaler:
    """
    Utility class to rescale image intensities to the range [0, 1],
    using (robust) min/max normalization.
    Optionally, the min/max bounds can be smoothed over a sliding window to avoid jitter.
    """

    def __init__(self, options):
        self.auto_hdr = options.auto_hdr
        if options.color:  # color reconstruction requires --auto_hdr to be enabled
            self.auto_hdr = True

        self.auto_hdr_min_percentile = options.auto_hdr_min_percentile
        assert(self.auto_hdr_min_percentile >= 0 and self.auto_hdr_min_percentile < 30)
        self.auto_hdr_max_percentile = options.auto_hdr_max_percentile
        assert(self.auto_hdr_max_percentile >= 70 and self.auto_hdr_max_percentile <= 100)
        self.auto_hdr_moving_average_size = options.auto_hdr_moving_average_size  # size of moving average window for auto hdr
        assert(self.auto_hdr_moving_average_size > 0 and self.auto_hdr_moving_average_size <= 100)
        self.auto_hdr_border = options.auto_hdr_border
        assert(self.auto_hdr_border >= 0 and self.auto_hdr_border < 50)

        self.intensity_bounds = deque()

        print('== Image rescaling ==')
        if self.auto_hdr:
            print('Will rescale image intensities to [0,1].')
            print('Min / max percentile: {:.1f}, {:.1f}.'.format(self.auto_hdr_min_percentile,
                                                                 self.auto_hdr_max_percentile))
            print('Sliding window size: {}.'.format(self.auto_hdr_moving_average_size))
            print('Ignoring outer border of width {} in computation of min / max.'.format(self.auto_hdr_border))

    def __call__(self, img):
        """
        param img: NumPy array taking values in [0, 1]
        """

        if self.auto_hdr:
            # adjust image dynamic range (i.e. its contrast)
            if len(self.intensity_bounds) > self.auto_hdr_moving_average_size:
                self.intensity_bounds.popleft()

            border = self.auto_hdr_border
            # Note: we ignore a few pixels on the outer image boundary when computing the robust min/max,
            # since those pixels tend to contain lots of outlier values (boundary effects),
            # leading to unstable min/max.
            rmin = robust_min(img[border:-border, border:-border].ravel(), self.auto_hdr_min_percentile)
            rmax = robust_max(img[border:-border, border:-border].ravel(), self.auto_hdr_max_percentile)
            self.intensity_bounds.append((rmin, rmax))
            mean_rmin = np.median([rmin for rmin, rmax in self.intensity_bounds])
            mean_rmax = np.median([rmax for rmin, rmax in self.intensity_bounds])
            img = (img.astype(np.float32) - mean_rmin) / (mean_rmax - mean_rmin)
            img = np.clip(img, 0.0, 1.0)

        return img


class ImageWriter:
    """
    Utility class to write images to disk.
    Also writes the image timestamps into a text file.
    """

    def __init__(self, options):

        self.output_folder = options.output_folder
        self.dataset_name = options.dataset_name
        self.color = options.color
        self.save_events = options.show_events
        print('== Image Writer ==')
        if self.output_folder:
            ensure_dir(self.output_folder)
            ensure_dir(join(self.output_folder, self.dataset_name))
            print('Will write images to: {}'.format(join(self.output_folder, self.dataset_name)))
            self.timestamps_file = open(join(self.output_folder, self.dataset_name, 'timestamps.txt'), 'a')

            if self.save_events:
                self.event_previews_folder = join(self.output_folder, self.dataset_name, 'events')
                ensure_dir(self.event_previews_folder)
                print('Will write event previews to: {}'.format(self.event_previews_folder))

            atexit.register(self.__cleanup__)
        else:
            print('Will not write images to disk.')

    def __call__(self, img, event_tensor_id, stamp=None, events=None):
        if not self.output_folder:
            return

        if self.save_events and events is not None:
            event_preview = make_event_preview(events, color=self.color)
            cv2.imwrite(join(self.event_previews_folder,
                             'events_{:010d}.png'.format(event_tensor_id)), event_preview)

        cv2.imwrite(join(self.output_folder, self.dataset_name,
                         'frame_{:010d}.png'.format(event_tensor_id)), img)
        if stamp is not None:
            self.timestamps_file.write('{:.18f}\n'.format(stamp))

    def __cleanup__(self):
        if self.output_folder:
            self.timestamps_file.close()


class ImageDisplay:
    """
    Utility class to display image reconstructions
    """

    def __init__(self, options):
        self.display = options.display
        self.show_events = options.show_events
        self.color = options.color

        self.window_name = 'Reconstruction'
        if self.show_events:
            self.window_name = 'Events | ' + self.window_name

        if self.display:
            cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)

        self.border = options.display_border_crop
        self.wait_time = options.display_wait_time

    def crop_outer_border(self, img, border):
        if self.border == 0:
            return img
        else:
            return img[border:-border, border:-border]

    def __call__(self, img, events=None):

        if not self.display:
            return

        img = self.crop_outer_border(img, self.border)

        if self.show_events:
            assert(events is not None)
            event_preview = make_event_preview(events, color=self.color)
            event_preview = self.crop_outer_border(event_preview, self.border)

        if self.show_events:
            img = np.hstack([event_preview, img])

        cv2.imshow(self.window_name, img)
        cv2.waitKey(self.wait_time)


def unsharp_mask(image, kernel_size=(5, 5), sigma=1.0, amount=1.0, threshold=0):
    """Return a sharpened version of the image, using an unsharp mask.
    Taken from: https://github.com/soroushj/python-opencv-numpy-example/blob/master/unsharpmask.py
    """
    # For details on unsharp masking, see:
    # https://en.wikipedia.org/wiki/Unsharp_masking
    # https://homepages.inf.ed.ac.uk/rbf/HIPR2/unsharp.htm
    blurred = cv2.GaussianBlur(image, kernel_size, sigma)
    sharpened = float(amount + 1) * image - float(amount) * blurred
    sharpened = np.maximum(sharpened, np.zeros(sharpened.shape))
    sharpened = np.minimum(sharpened, 255 * np.ones(sharpened.shape))
    sharpened = sharpened.round().astype(np.uint8)
    if threshold > 0:
        low_contrast_mask = np.absolute(image - blurred) < threshold
        np.copyto(sharpened, image, where=low_contrast_mask)
    return sharpened


class ImageFilter:
    """
    Utility class to perform some basic filtering on reconstructed images.
    """

    def __init__(self, options):
        self.unsharp_mask_amount = options.unsharp_mask_amount
        self.unsharp_mask_sigma = options.unsharp_mask_sigma
        self.bilateral_filter_sigma = options.bilateral_filter_sigma

    def __call__(self, img):

        if self.unsharp_mask_amount > 0:
            with Timer('Unsharp mask (sigma={:.2f}, amount={:.1f})'.format(self.unsharp_mask_sigma, self.unsharp_mask_amount)):
                filtered_img = unsharp_mask(img, sigma=self.unsharp_mask_sigma, amount=self.unsharp_mask_amount)
                img = filtered_img

        if self.bilateral_filter_sigma:
            with Timer('Bilateral filter (sigma={:.2f})'.format(self.bilateral_filter_sigma)):
                filtered_img = np.zeros_like(img)
                filtered_img = cv2.bilateralFilter(
                    img, 5, 25.0 * self.bilateral_filter_sigma, 25.0 * self.bilateral_filter_sigma)
                img = filtered_img

        return img


def optimal_crop_size(max_size, max_subsample_factor, safety_margin=0):
    """ Find the optimal crop size for a given max_size and subsample_factor.
        The optimal crop size is the smallest integer which is greater or equal than max_size,
        while being divisible by 2^max_subsample_factor.
    """
    crop_size = int(pow(2, max_subsample_factor) * ceil(max_size / pow(2, max_subsample_factor)))
    crop_size += safety_margin * pow(2, max_subsample_factor)
    return crop_size


class CropParameters:
    """ Helper class to compute and store useful parameters for pre-processing and post-processing
        of images in and out of E2VID.
        Pre-processing: finding the best image size for the network, and padding the input image with zeros
        Post-processing: Crop the output image back to the original image size
    """

    def __init__(self, width, height, num_encoders, safety_margin=0):

        self.height = height
        self.width = width
        self.num_encoders = num_encoders
        self.width_crop_size = optimal_crop_size(self.width, num_encoders, safety_margin)
        self.height_crop_size = optimal_crop_size(self.height, num_encoders, safety_margin)

        self.padding_top = ceil(0.5 * (self.height_crop_size - self.height))
        self.padding_bottom = floor(0.5 * (self.height_crop_size - self.height))
        self.padding_left = ceil(0.5 * (self.width_crop_size - self.width))
        self.padding_right = floor(0.5 * (self.width_crop_size - self.width))
        self.pad = ZeroPad2d((self.padding_left, self.padding_right, self.padding_top, self.padding_bottom))

        self.cx = floor(self.width_crop_size / 2)
        self.cy = floor(self.height_crop_size / 2)

        self.ix0 = self.cx - floor(self.width / 2)
        self.ix1 = self.cx + ceil(self.width / 2)
        self.iy0 = self.cy - floor(self.height / 2)
        self.iy1 = self.cy + ceil(self.height / 2)


def shift_image(X, dx, dy):
    X = np.roll(X, dy, axis=0)
    X = np.roll(X, dx, axis=1)
    if dy > 0:
        X[:dy, :] = 0
    elif dy < 0:
        X[dy:, :] = 0
    if dx > 0:
        X[:, :dx] = 0
    elif dx < 0:
        X[:, dx:] = 0
    return X


def upsample_color_image(grayscale_highres, color_lowres_bgr, colorspace='LAB'):
    """
    Generate a high res color image from a high res grayscale image, and a low res color image,
    using the trick described in:
    http://www.planetary.org/blogs/emily-lakdawalla/2013/04231204-image-processing-colorizing-images.html
    """
    assert(len(grayscale_highres.shape) == 2)
    assert(len(color_lowres_bgr.shape) == 3 and color_lowres_bgr.shape[2] == 3)

    if colorspace == 'LAB':
        # convert color image to LAB space
        lab = cv2.cvtColor(src=color_lowres_bgr, code=cv2.COLOR_BGR2LAB)
        # replace lightness channel with the highres image
        lab[:, :, 0] = grayscale_highres
        # convert back to BGR
        color_highres_bgr = cv2.cvtColor(src=lab, code=cv2.COLOR_LAB2BGR)
    elif colorspace == 'HSV':
        # convert color image to HSV space
        hsv = cv2.cvtColor(src=color_lowres_bgr, code=cv2.COLOR_BGR2HSV)
        # replace value channel with the highres image
        hsv[:, :, 2] = grayscale_highres
        # convert back to BGR
        color_highres_bgr = cv2.cvtColor(src=hsv, code=cv2.COLOR_HSV2BGR)
    elif colorspace == 'HLS':
        # convert color image to HLS space
        hls = cv2.cvtColor(src=color_lowres_bgr, code=cv2.COLOR_BGR2HLS)
        # replace lightness channel with the highres image
        hls[:, :, 1] = grayscale_highres
        # convert back to BGR
        color_highres_bgr = cv2.cvtColor(src=hls, code=cv2.COLOR_HLS2BGR)

    return color_highres_bgr


def merge_channels_into_color_image(channels):
    """
    Combine a full resolution grayscale reconstruction and four color channels at half resolution
    into a color image at full resolution.

    :param channels: dictionary containing the four color reconstructions (at quarter resolution),
                     and the full resolution grayscale reconstruction.
    :return a color image at full resolution
    """

    assert('R' in channels)
    assert('G' in channels)
    assert('W' in channels)
    assert('B' in channels)
    assert('grayscale' in channels)

    # upsample each channel independently
    for channel in ['R', 'G', 'W', 'B']:
        channels[channel] = cv2.resize(channels[channel], dsize=None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    # Shift the channels so that they all have the same origin
    channels['B'] = shift_image(channels['B'], dx=1, dy=1)
    channels['G'] = shift_image(channels['G'], dx=1, dy=0)
    channels['W'] = shift_image(channels['W'], dx=0, dy=1)

    # reconstruct the color image at half the resolution using the reconstructed channels RGBW
    reconstruction_bgr = np.dstack([channels['B'],
                                    0.5 * (channels['G'] + channels['W']),
                                    channels['R']])

    reconstruction_bgr = (255.0 * reconstruction_bgr).astype(np.uint8)
    reconstruction_grayscale = (255.0 * channels['grayscale']).astype(np.uint8)

    # combine the full res grayscale resolution with the low res to get a full res color image
    return upsample_color_image(reconstruction_grayscale, reconstruction_bgr)


def events_to_voxel_grid(events, num_bins, width, height):
    """
    Build a voxel grid with bilinear interpolation in the time domain from a set of events.

    :param events: a [N x 4] NumPy array containing one event per row in the form: [timestamp, x, y, polarity]
    :param num_bins: number of bins in the temporal axis of the voxel grid
    :param width, height: dimensions of the voxel grid
    """

    assert(events.shape[1] == 4)
    assert(num_bins > 0)
    assert(width > 0)
    assert(height > 0)

    voxel_grid = np.zeros((num_bins, height, width), np.float32).ravel()

    # normalize the event timestamps so that they lie between 0 and num_bins
    last_stamp = events[-1, 0]
    first_stamp = events[0, 0]
    deltaT = last_stamp - first_stamp

    if deltaT == 0:
        deltaT = 1.0

    events[:, 0] = (num_bins - 1) * (events[:, 0] - first_stamp) / deltaT
    ts = events[:, 0]
    xs = events[:, 1].astype(np.int)
    ys = events[:, 2].astype(np.int)
    pols = events[:, 3]
    pols[pols == 0] = -1  # polarity should be +1 / -1

    tis = ts.astype(np.int)
    dts = ts - tis
    vals_left = pols * (1.0 - dts)
    vals_right = pols * dts

    valid_indices = tis < num_bins
    np.add.at(voxel_grid, xs[valid_indices] + ys[valid_indices] * width
              + tis[valid_indices] * width * height, vals_left[valid_indices])

    valid_indices = (tis + 1) < num_bins
    np.add.at(voxel_grid, xs[valid_indices] + ys[valid_indices] * width
              + (tis[valid_indices] + 1) * width * height, vals_right[valid_indices])

    voxel_grid = np.reshape(voxel_grid, (num_bins, height, width))

    return voxel_grid


def events_to_voxel_grid_pytorch(events, num_bins, width, height, device):
    """
    Build a voxel grid with bilinear interpolation in the time domain from a set of events.

    :param events: a [N x 4] NumPy array containing one event per row in the form: [timestamp, x, y, polarity]
    :param num_bins: number of bins in the temporal axis of the voxel grid
    :param width, height: dimensions of the voxel grid
    :param device: device to use to perform computations
    :return voxel_grid: PyTorch event tensor (on the device specified)
    """

    DeviceTimer = CudaTimer if device.type == 'cuda' else Timer

    assert(events.shape[1] == 4)
    assert(num_bins > 0)
    assert(width > 0)
    assert(height > 0)

    with torch.no_grad():

        events_torch = torch.from_numpy(events)
        with DeviceTimer('Events -> Device (voxel grid)'):
            events_torch = events_torch.to(device)

        with DeviceTimer('Voxel grid voting'):
            voxel_grid = torch.zeros(num_bins, height, width, dtype=torch.float32, device=device).flatten()

            # normalize the event timestamps so that they lie between 0 and num_bins
            last_stamp = events_torch[-1, 0]
            first_stamp = events_torch[0, 0]
            deltaT = last_stamp - first_stamp

            if deltaT == 0:
                deltaT = 1.0

            events_torch[:, 0] = (num_bins - 1) * (events_torch[:, 0] - first_stamp) / deltaT
            ts = events_torch[:, 0]
            xs = events_torch[:, 1].long()
            ys = events_torch[:, 2].long()
            pols = events_torch[:, 3].float()
            pols[pols == 0] = -1  # polarity should be +1 / -1

            tis = torch.floor(ts)
            tis_long = tis.long()
            dts = ts - tis
            vals_left = pols * (1.0 - dts.float())
            vals_right = pols * dts.float()

            valid_indices = tis < num_bins
            valid_indices &= tis >= 0
            voxel_grid.index_add_(dim=0,
                                  index=xs[valid_indices] + ys[valid_indices]
                                  * width + tis_long[valid_indices] * width * height,
                                  source=vals_left[valid_indices])

            valid_indices = (tis + 1) < num_bins
            valid_indices &= tis >= 0

            voxel_grid.index_add_(dim=0,
                                  index=xs[valid_indices] + ys[valid_indices] * width
                                  + (tis_long[valid_indices] + 1) * width * height,
                                  source=vals_right[valid_indices])

        voxel_grid = voxel_grid.view(num_bins, height, width)

    return voxel_grid
