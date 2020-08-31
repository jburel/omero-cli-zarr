import argparse
from collections import defaultdict
from fileinput import input
from typing import Dict, List, Set, Tuple

import numpy as np
import ome_zarr
import omero.clients  # noqa
from omero.model import MaskI
from omero.rtypes import unwrap
from zarr.convenience import open as zarr_open

# Mapping of dimension names to axes in the Zarr
DIMENSION_ORDER: Dict[str, int] = {
    "T": 0,
    "C": 1,
    "Z": 2,
    "Y": 3,
    "X": 4,
}

MASK_DTYPE_SIZE: Dict[int, np.dtype] = {
    1: np.bool,
    8: np.int8,
    16: np.int16,
    32: np.int32,
    64: np.int64,
}


def image_masks_to_zarr(image: omero.gateway.Image, args: argparse.Namespace) -> None:

    conn = image._conn
    roi_service = conn.getRoiService()
    result = roi_service.findByImage(image.id, None)

    masks = {}
    shape_count = 0
    for roi in result.rois:
        mask_shapes = []
        for s in roi.copyShapes():
            if isinstance(s, MaskI):
                mask_shapes.append(s)

        if len(mask_shapes) > 0:
            masks[roi.id.val] = mask_shapes
            shape_count += len(mask_shapes)

    print(f"Found {shape_count} mask shapes in {len(masks)} ROIs")

    dtype = MASK_DTYPE_SIZE[int(args.label_bits)]

    if args.style == "labeled" and args.label_bits == "1":
        print("Boolean type makes no sense for labeled. Using 64")
        dtype = MASK_DTYPE_SIZE[64]

    if masks:

        saver = MaskSaver(image, dtype, args.label_path, args.style, args.source_image)

        if args.style == "split":
            for (roi_id, roi) in masks.items():
                saver.save([roi], str(roi_id))
        else:
            if args.label_map:

                label_map = defaultdict(list)
                roi_map = {}
                for (roi_id, roi) in masks.items():
                    roi_map[roi_id] = roi

                try:
                    for line in input(args.label_map):
                        line = line.strip()
                        sid, name, roi = line.split(",")
                        label_map[name].append(roi_map[int(roi)])
                except Exception as e:
                    print(f"Error parsing {args.label_map}: {e}")

                for name, values in label_map.items():
                    print(f"Label map: {name} (count: {len(values)})")
                    saver.save(values, name)
            else:
                saver.save(list(masks.values()), args.label_name)
    else:
        print("No masks found on Image")


class MaskSaver:
    """
    Action class containing the parameters needed for mapping from
    masks to zarr groups/arrays.
    """

    def __init__(
        self,
        image: omero.gateway.Image,
        dtype: np.dtype,
        path: str = "labels",
        style: str = "6d",
        source: str = "..",
    ) -> None:
        self.image = image
        self.dtype = dtype
        self.path = path
        self.style = style
        self.size_t = image.getSizeT()
        self.size_c = image.getSizeC()
        self.size_z = image.getSizeZ()
        self.size_y = image.getSizeY()
        self.size_x = image.getSizeX()
        self.source_image = source
        self.image_shape = (
            self.size_t,
            self.size_c,
            self.size_z,
            self.size_y,
            self.size_x,
        )

    def save(self, masks: List[omero.model.Shape], name: str) -> None:

        # Figure out whether we can flatten some dimensions
        unique_dims: Dict[str, Set[int]] = {
            "T": {unwrap(mask.theT) for shapes in masks for mask in shapes},
            "C": {unwrap(mask.theC) for shapes in masks for mask in shapes},
            "Z": {unwrap(mask.theZ) for shapes in masks for mask in shapes},
        }
        ignored_dimensions: Set[str] = set()
        print(f"Unique dimensions: {unique_dims}")

        for d in "TCZ":
            if unique_dims[d] == {None}:
                ignored_dimensions.add(d)

        filename = f"{self.image.id}.zarr"

        # Verify that we are linking this mask to a real ome-zarr
        source_image = self.source_image
        source_image_link = self.source_image
        if source_image is None:
            # Assume that we're using the output directory
            source_image = filename
            source_image_link = "../.."  # Drop "labels/0"

        src = ome_zarr.parse_url(source_image)
        assert src

        root = zarr_open(filename)
        # TODO: Use ome-zarr here to write a multiscale?
        if self.path in root.group_keys():
            out_labels = getattr(root, self.path)
        else:
            out_labels = root.create_group(self.path)

        _mask_shape: List[int] = list(self.image_shape)
        for d in ignored_dimensions:
            _mask_shape[DIMENSION_ORDER[d]] = 1
            mask_shape: Tuple[int, ...] = tuple(_mask_shape)
        del _mask_shape
        print(f"Ignoring dimensions {ignored_dimensions}")

        if self.style in ("labeled", "split"):

            za = out_labels.create_dataset(
                name,
                shape=mask_shape,
                chunks=(1, 1, 1, self.size_y, self.size_x),
                dtype=self.dtype,
                overwrite=True,
            )

            self.masks_to_labels(
                masks, mask_shape, ignored_dimensions, check_overlaps=True, labels=za,
            )

        else:
            assert self.style == "6d"
            za = out_labels.create_dataset(
                name,
                shape=tuple([len(masks)] + list(mask_shape)),
                chunks=(1, 1, 1, 1, self.size_y, self.size_x),
                dtype=self.dtype,
                overwrite=True,
            )

            self.stack_masks(
                masks, mask_shape, za, ignored_dimensions, check_overlaps=True,
            )

        out_labels[name].attrs["image"] = {
            "array": source_image_link,
            "source": {
                # 'ts': [],
                # 'cs': [],
                # 'zs': [],
                # 'ys': [],
                # 'xs': [],
            },
        }

        print(f"Created {filename}/{self.path}/{name}")
        attrs = out_labels.attrs.asdict()
        # TODO: could temporarily support "masks" here as well
        if "labels" in attrs:
            if name not in attrs["labels"]:
                attrs["labels"].append(name)
        else:
            attrs["labels"] = [name]
        out_labels.attrs.update(attrs)

    def _mask_to_binim_yx(
        self, mask: omero.model.Shape
    ) -> Tuple[np.ndarray, Tuple[int, ...]]:
        """
        :param mask MaskI: An OMERO mask

        :return: tuple of
                - Binary mask
                - (T, C, Z, Y, X, w, h) tuple of mask settings (T, C, Z may be
                None)

        TODO: Move to https://github.com/ome/omero-rois/
        """

        t = unwrap(mask.theT)
        c = unwrap(mask.theC)
        z = unwrap(mask.theZ)

        x = int(mask.x.val)
        y = int(mask.y.val)
        w = int(mask.width.val)
        h = int(mask.height.val)

        mask_packed = mask.getBytes()
        # convert bytearray into something we can use
        intarray = np.fromstring(mask_packed, dtype=np.uint8)
        binarray = np.unpackbits(intarray).astype(self.dtype)
        # truncate and reshape
        binarray = np.reshape(binarray[: (w * h)], (h, w))

        return binarray, (t, c, z, y, x, h, w)

    def _get_indices(
        self, ignored_dimensions: Set[str], d: str, d_value: int, d_size: int
    ) -> List[int]:
        """
        Figures out which Z/C/T-planes a mask should be copied to
        """
        if d in ignored_dimensions:
            return [0]
        if d_value is not None:
            return [d_value]
        return range(d_size)

    def masks_to_labels(
        self,
        masks: List[omero.model.Mask],
        mask_shape: Tuple[int, ...],
        ignored_dimensions: Set[str] = None,
        check_overlaps: bool = True,
        labels: np.ndarray = None,
    ) -> np.ndarray:
        """
        :param masks [MaskI]: Iterable container of OMERO masks
        :param mask_shape 5-tuple: the image dimensions (T, C, Z, Y, X), taking
            into account `ignored_dimensions`
        :param ignored_dimensions set(char): Ignore these dimensions and set
            size to 1
        :param check_overlaps bool: Whether to check for overlapping masks or
            not
        :param labels nd-array: The optional output array, pass this if you
            have already created the array and want to fill it.

        :return: Label image with size `mask_shape`

        TODO: Move to https://github.com/ome/omero-rois/
        """

        # FIXME: hard-coded dimensions
        assert len(mask_shape) > 3
        size_t: int = mask_shape[0]
        size_c: int = mask_shape[1]
        size_z: int = mask_shape[2]
        ignored_dimensions = ignored_dimensions or set()

        if not labels:
            # TODO: Set np.int size based on number of labels
            labels = np.zeros(mask_shape, np.int64)

        for d in "TCZYX":
            if d in ignored_dimensions:
                assert (
                    labels.shape[DIMENSION_ORDER[d]] == 1
                ), f"Ignored dimension {d} should be size 1"
            assert (
                labels.shape == mask_shape
            ), f"Invalid label shape: {labels.shape}, expected {mask_shape}"

        fillColors = {}
        for count, shapes in enumerate(masks):
            # All shapes same color for each ROI
            print(count)
            for mask in shapes:
                # Unused metadata: the{ZTC}, x, y, width, height, textValue
                if mask.fillColor:
                    fillColors[count + 1] = unwrap(mask.fillColor)
                binim_yx, (t, c, z, y, x, h, w) = self._mask_to_binim_yx(mask)
                for i_t in self._get_indices(ignored_dimensions, "T", t, size_t):
                    for i_c in self._get_indices(ignored_dimensions, "C", c, size_c):
                        for i_z in self._get_indices(
                            ignored_dimensions, "Z", z, size_z
                        ):
                            if check_overlaps and np.any(
                                np.logical_and(
                                    labels[
                                        i_t, i_c, i_z, y : (y + h), x : (x + w)
                                    ].astype(np.bool),
                                    binim_yx,
                                )
                            ):
                                raise Exception(
                                    f"Mask {count} overlaps " "with existing labels"
                                )
                            # ADD to the array, so zeros in our binarray don't
                            # wipe out previous masks
                            labels[i_t, i_c, i_z, y : (y + h), x : (x + w)] += (
                                binim_yx * (count + 1)  # Prevent zeroing
                            )

        labels.attrs["color"] = fillColors
        return labels

    def stack_masks(
        self,
        masks: List[omero.model.Mask],
        mask_shape: Tuple[int, ...],
        target: np.ndarray,
        ignored_dimensions: Set[str] = None,
        check_overlaps: bool = True,
    ) -> None:
        """
        :param masks [MaskI]: Iterable container of OMERO masks
        :param mask_shape 5-tuple: the image dimensions (T, C, Z, Y, X), taking
            into account `ignored_dimensions`
        :param target nd-array: The output array, pass this if you
            have already created the array and want to fill it.
        :param ignored_dimensions set(char): Ignore these dimensions and set
            size to 1.
        :param check_overlaps bool: Whether to check for overlapping masks or
            not

        :return: Array with one extra dimension than `mask_shape`

        TODO: Move to https://github.com/ome/omero-rois/
        """

        assert len(mask_shape) > 3
        size_t: int = mask_shape[0]
        size_c: int = mask_shape[1]
        size_z: int = mask_shape[2]
        if ignored_dimensions is None:
            ignored_dimensions = set()

        if not target:
            raise Exception("No target")

        for d in "TCZYX":
            if d in ignored_dimensions:
                assert (
                    target.shape[DIMENSION_ORDER[d] + 1] == 1
                ), f"Ignored dimension {d} should be size 1"
            assert target.shape == tuple(
                [len(masks)] + list(mask_shape)
            ), f"Invalid label shape: {target.shape}, expected {mask_shape}"

        for count, shapes in enumerate(masks):
            # All shapes same color for each ROI
            for mask in shapes:
                binim_yx, (t, c, z, y, x, h, w) = self._mask_to_binim_yx(mask)
                for i_t in self._get_indices(ignored_dimensions, "T", t, size_t):
                    for i_c in self._get_indices(ignored_dimensions, "C", c, size_c):
                        for i_z in self._get_indices(
                            ignored_dimensions, "Z", z, size_z
                        ):
                            target[
                                count, i_t, i_c, i_z, y : (y + h), x : (x + w)
                            ] += binim_yx  # Here one could assign probabilities
