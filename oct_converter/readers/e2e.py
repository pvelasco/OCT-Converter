from __future__ import annotations

import time
import warnings
from collections import defaultdict
from datetime import datetime
from itertools import chain
from pathlib import Path

import numpy as np
from construct.core import StreamError

from oct_converter.image_types import FundusImageWithMetaData, OCTVolumeWithMetaData
from oct_converter.readers.binary_structs import e2e_binary


class E2E(object):
    """Class for extracting data from Heidelberg's .e2e file format.

    Notes:
        Mostly based on description of .e2e file format here:
        https://bitbucket.org/uocte/uocte/wiki/Heidelberg%20File%20Format.

    Attributes:
        filepath: path to .e2e file for reading.
    """

    def __init__(self, filepath: str | Path) -> None:
        self.filepath = Path(filepath)
        if not self.filepath.exists():
            raise FileNotFoundError(self.filepath)

        self.power = pow(2, 10)
        self.sex = None
        self.first_name = None
        self.surname = None
        self.acquisition_date = None
        self.birthdate = None
        self.pixel_spacing = None
        self.patient_id = None

        # get initial directory structure
        with open(self.filepath, "rb") as f:
            raw = f.read(21)
            if raw == b"E2EMultipleVolumeFile":
                self.byte_skip = 64
            else:
                self.byte_skip = 0
            f.seek(self.byte_skip)
            raw = f.read(36)

            header = e2e_binary.header_structure.parse(raw)
            raw = f.read(52)
            main_directory = e2e_binary.main_directory_structure.parse(raw)

            # traverse list of main directories in first pass
            self.directory_stack = []

            current = main_directory.current
            while current != 0:
                self.directory_stack.append(current)
                f.seek(current + self.byte_skip)
                raw = f.read(52)
                directory_chunk = e2e_binary.main_directory_structure.parse(raw)
                current = directory_chunk.prev

    @staticmethod
    def _convert_ole_date_to_yyyymmdd(ole_date: float | int) -> str | None:
        """Convert Windows OLE Automation date to YYYYMMDD string format.

        The birthdate field in .e2e files can be stored in multiple formats:
        1. YYYYMMDD integer (e.g., 19850315)
        2. Fixed-point integer representation of OLE date (Int32 / 100000 = OLE date)
        3. Direct OLE date float (rare)

        Windows OLE dates represent days since 1/1/1900 where 1/1/1900 = 0.0.

        Args:
            ole_date: Birth date value from .e2e file (int or float).

        Returns:
            Date string in YYYYMMDD format, or None if conversion fails or date is invalid.

        References:
            - https://github.com/neurodial/LibE2E/blob/master/E2E/dataelements/patientdataelement.h#L47
            - https://github.com/neurodial/LibOctData/blob/master/octdata/datastruct/date.cpp
            - https://github.com/neurodial/LibE2E/issues/5
        """
        # Check if birthdate was stored as YYYYMMDD integer (encountered in some files)
        if len(str(ole_date)) == 8:
            return str(ole_date)

        try:
            if ole_date > 0:
                # Check if this is a fixed-point integer (Int32 scaled by 100000)
                # Empirically observed in .e2e files: birthdate stored as Int32un
                # where actual_ole_date = int_value / 100000
                if isinstance(ole_date, int) and ole_date > 100000:
                    ole_date = ole_date / 100000.0

                # Convert Windows OLE date to Unix timestamp
                # 25569 = days between 1/1/1900 and Unix epoch (1/1/1970)
                # 86400 = seconds per day
                unix_time = (ole_date - 25569) * 86400
                return datetime.fromtimestamp(unix_time).strftime("%Y%m%d")
            else:
                return None
        except (ValueError, OSError):
            # ValueError: date out of range
            # OSError: invalid timestamp on some platforms
            return None

    @staticmethod
    def _parse_patient_data_from_file(f, chunk):
        """Parse patient data from chunk type 9.

        E2E files use different structure sizes for patient data depending on
        scanner model/software version. This method tries both variants.

        Args:
            f: Open file handle positioned after the chunk header
            chunk: Parsed chunk structure with chunk.size attribute

        Returns:
            Parsed patient data object from construct, or None if parsing fails.

        Note:
            - 127-byte structure: birthdate as Int32un (4 bytes) - most common
            - 131-byte structure: birthdate as Float64l (8 bytes) - some scanners
            - chunk.size is NOT reliable for determining structure type due to padding,
              so we try the 127-byte version first, then fall back to 131-byte
        """
        # Save position and read maximum bytes needed
        pos = f.tell()
        raw = f.read(max(127, chunk.size))

        # Try 127-byte structure first (most common)
        try:
            patient_data = e2e_binary.patient_id_structure.parse(raw[:127])
            return patient_data
        except Exception:
            # If 127-byte fails, try 131-byte structure
            try:
                f.seek(pos)
                raw = f.read(131)
                patient_data = e2e_binary.patient_id_structure_v2.parse(raw)
                return patient_data
            except Exception:
                return None

    def _parse_patient_data_chunk(self, f, chunk):
        """Parse patient data from chunk type 9 and set instance variables.

        Reads and parses the patient_id_structure from the file, then sets
        instance variables (sex, first_name, surname, patient_id, birthdate).

        Args:
            f: Open file handle positioned after the chunk header
            chunk: Parsed chunk structure (not currently used, for future extensibility)

        Returns:
            Parsed patient data object from construct, or None if parsing fails.
        """
        patient_data = self._parse_patient_data_from_file(f, chunk)
        if patient_data is not None:
            # Set instance variables
            self.sex = patient_data.sex
            self.first_name = patient_data.first_name
            self.surname = patient_data.surname
            self.patient_id = patient_data.patient_id
            self.birthdate = self._convert_ole_date_to_yyyymmdd(patient_data.birthdate)

        return patient_data

    def read_oct_volume(
        self,
        legacy_intensity_transform: bool = False,
        scalex: float = 0.01,
        slice_thickness: float = 0.05,
    ) -> list[OCTVolumeWithMetaData]:
        """Reads OCT data.

        Args:
             legacy_intensity_transform: if True, use intensity transform used in v<=0.5.7. Defaults to False.
             scalex: Manually set scale of x axis
             slice_thickness: Manually set scale of z axis

        Returns:
            A list of OCTVolumeWithMetaData.
        """

        def _make_lut():
            LUT = []
            for i in range(0, pow(2, 16)):
                LUT.append(self.uint16_to_ufloat16(i))
            return np.array(LUT)

        LUT = _make_lut()

        with open(self.filepath, "rb") as f:
            # get all subdirectories
            chunk_stack = []
            volume_dict = {}
            for position in self.directory_stack:
                f.seek(position + self.byte_skip)
                raw = f.read(52)
                directory_chunk = e2e_binary.main_directory_structure.parse(raw)

                for ii in range(directory_chunk.num_entries):
                    raw = f.read(44)
                    chunk = e2e_binary.sub_directory_structure.parse(raw)
                    volume_string = "{}_{}_{}".format(
                        chunk.patient_db_id, chunk.study_id, chunk.series_id
                    )
                    if volume_string not in volume_dict.keys():
                        volume_dict[volume_string] = chunk.slice_id / 2
                    elif chunk.slice_id / 2 > volume_dict[volume_string]:
                        volume_dict[volume_string] = chunk.slice_id / 2

                    if chunk.start > chunk.pos:
                        chunk_stack.append([chunk.start, chunk.size])

            # initalise dict to hold all the image volumes
            volume_array_dict = {}
            volume_array_dict_additional = (
                {}
            )  # for storage of slices not caught by extraction
            laterality_dict = {}
            laterality = None
            for volume, num_slices in volume_dict.items():
                if num_slices > 0:
                    # num_slices + 1 here due to evidence that a slice was being missed off the end in extraction
                    volume_array_dict[volume] = [0] * int(num_slices + 1)

            contour_dict = defaultdict(lambda: defaultdict(dict))

            # traverse all chunks and extract slices
            for start, pos in chunk_stack:
                f.seek(start + self.byte_skip)
                raw = f.read(60)
                try:
                    # Heidelberg's updated anonymization seems to cause problems with
                    # some chunks. Observed problems include an empty raw and problems
                    # with undecodable bytes. For now, these chunks are skipped...
                    chunk = e2e_binary.chunk_structure.parse(raw)
                except Exception:
                    continue

                if chunk.type == 9:  # patient data
                    self._parse_patient_data_chunk(f, chunk)

                elif chunk.type == 10004:  # bscan metadata
                    raw = f.read(104)
                    bscan_metadata = e2e_binary.bscan_metadata.parse(raw)

                    windowsTicks = bscan_metadata.acquisitionTime
                    windowsTicksToUnixFactor = 10000000
                    secToUnixEpechFromWindowsTicks = 11644473600
                    unixtime = (
                        windowsTicks / windowsTicksToUnixFactor
                        - secToUnixEpechFromWindowsTicks
                    )
                    utc_time = time.gmtime(unixtime)
                    dt_time = datetime.fromtimestamp(time.mktime(utc_time))
                    if self.acquisition_date is None:
                        self.acquisition_date = dt_time
                    if self.pixel_spacing is None:
                        # scaley found, x and z not yet found in file
                        self.pixel_spacing = [
                            scalex,
                            bscan_metadata.scaley,
                            slice_thickness,
                        ]

                elif chunk.type == 3:  # scan preamble data
                    raw = f.read(chunk.size)
                    try:
                        pre_data = e2e_binary.pre_data.parse(raw)
                        if pre_data.laterality in ["R", "L"]:
                            laterality = pre_data.laterality
                    except Exception:
                        laterality = None
                    volume_string = "{}_{}_{}".format(
                        chunk.patient_db_id, chunk.study_id, chunk.series_id
                    )
                    if laterality and (volume_string not in laterality_dict):
                        laterality_dict[volume_string] = laterality

                elif chunk.type == 10019:  # contour data
                    raw = f.read(16)
                    contour_data = e2e_binary.contour_structure.parse(raw)

                    if contour_data.width > 0:
                        volume_string = "{}_{}_{}".format(
                            chunk.patient_db_id, chunk.study_id, chunk.series_id
                        )
                        slice_id = int(chunk.slice_id / 2)
                        contour_name = f"contour{contour_data.id}"
                        try:
                            raw_volume = np.frombuffer(
                                f.read(contour_data.width * 4), dtype=np.float32
                            )
                            contour = np.array(raw_volume)
                            max_float = np.finfo(np.float32).max
                            contour[(contour < 1e-9) | (contour == max_float)] = np.nan
                        except Exception as e:
                            warnings.warn(
                                (
                                    f"Could not read contour "
                                    f"image id {volume_string}"
                                    f"contour name {contour_name} "
                                    f"slice id {slice_id}."
                                ),
                                UserWarning,
                            )
                        else:
                            (
                                contour_dict[volume_string][contour_name][slice_id]
                            ) = contour

                elif chunk.type == 1073741824:  # image data
                    raw = f.read(20)
                    image_data = e2e_binary.image_structure.parse(raw)

                    if chunk.ind == 1:  # oct data
                        count = image_data.height * image_data.width
                        if count == 0:
                            break
                        raw_volume = np.fromfile(f, dtype=np.uint16, count=count)
                        volume_string = "{}_{}_{}".format(
                            chunk.patient_db_id, chunk.study_id, chunk.series_id
                        )
                        try:
                            image = LUT[raw_volume].reshape(
                                image_data.height, image_data.width
                            )
                        except Exception:
                            warnings.warn(
                                (
                                    f"Could not reshape image id {volume_string} with "
                                    f"{len(LUT[raw_volume])} elements into a "
                                    f"{image_data.height}x"
                                    f"{image_data.width} array"
                                ),
                                UserWarning,
                            )
                        else:
                            if legacy_intensity_transform:
                                image = pow(image, 1.0 / 2.4)
                            else:
                                image = self.vol_intensity_transform(image)

                            if volume_string in volume_array_dict.keys():
                                volume_array_dict[volume_string][
                                    int(chunk.slice_id / 2)
                                ] = image
                            else:
                                # try to capture these additional images
                                if volume_string in volume_array_dict_additional.keys():
                                    volume_array_dict_additional[volume_string].append(
                                        image
                                    )
                                else:
                                    volume_array_dict_additional[volume_string] = [
                                        image
                                    ]

            contour_data = {}
            for volume_id, contours in contour_dict.items():
                if volume_id in volume_dict:
                    num_slices = int(volume_dict[volume_id]) + 1
                else:
                    num_slices = None
                contour_data[volume_id] = {
                    k: [None] * (num_slices or len(v)) for k, v in contours.items()
                }

                for contour_name, contour_values in contours.items():
                    for slice_id, contour in contour_values.items():
                        (contour_data[volume_id][contour_name][slice_id]) = contour

            # Read metadata to attach to OCTVolumeWithMetaData
            metadata = self.read_all_metadata()

            oct_volumes = []
            for key, volume in chain(
                volume_array_dict.items(), volume_array_dict_additional.items()
            ):
                # remove any initalised volumes that never had image data attached
                volume = [slc for slc in volume if not isinstance(slc, int)]
                if volume is None or len(volume) == 0:
                    continue
                oct_volumes.append(
                    OCTVolumeWithMetaData(
                        volume=volume,
                        patient_id=self.patient_id,
                        first_name=self.first_name,
                        surname=self.surname,
                        sex=self.sex,
                        patient_dob=self.birthdate,
                        acquisition_date=self.acquisition_date,
                        volume_id=key,
                        laterality=laterality_dict.get(key),
                        contours=contour_data.get(key),
                        pixel_spacing=self.pixel_spacing,
                        metadata=metadata,
                    )
                )

        return oct_volumes

    def read_fundus_image(
        self,
        extract_scan_repeats: bool = False,
        scalex: float = 0.01,
    ) -> list[FundusImageWithMetaData]:
        """Reads fundus data.

        Args:
            extract_scan_repeats: if True, extract all fundus images, including those that appear repeated. Defaults to False.
            scalex: Manually set scale of x axis

        Returns:
            A sequence of FundusImageWithMetaData.
        """
        with open(self.filepath, "rb") as f:
            # traverse in second pass and  get all subdirectories
            chunk_stack = []
            for position in self.directory_stack:
                f.seek(position + self.byte_skip)
                raw = f.read(52)
                directory_chunk = e2e_binary.main_directory_structure.parse(raw)

                for ii in range(directory_chunk.num_entries):
                    raw = f.read(44)
                    chunk = e2e_binary.sub_directory_structure.parse(raw)
                    if chunk.start > chunk.pos:
                        chunk_stack.append([chunk.start, chunk.size])

            # initalise dict to hold all the image volumes
            image_array_dict = {}
            laterality_dict = {}
            laterality = None

            # traverse all chunks and extract slices
            for start, pos in chunk_stack:
                f.seek(start + self.byte_skip)
                raw = f.read(60)
                try:
                    # Heidelberg's updated anonymization seems to cause problems with
                    # some chunks. Observed problems include an empty raw and problems
                    # with undecodable bytes. For now, these chunks are skipped...
                    chunk = e2e_binary.chunk_structure.parse(raw)
                except Exception:
                    continue

                if chunk.type == 9:  # patient data
                    self._parse_patient_data_chunk(f, chunk)

                elif chunk.type == 3:  # scan preamble data
                    raw = f.read(chunk.size)
                    try:
                        pre_data = e2e_binary.pre_data.parse(raw)
                        if pre_data.laterality in ["R", "L"]:
                            laterality = pre_data.laterality
                    except Exception:
                        laterality = None
                    volume_string = "{}_{}_{}".format(
                        chunk.patient_db_id, chunk.study_id, chunk.series_id
                    )
                    if laterality and (volume_string not in laterality_dict):
                        laterality_dict[volume_string] = laterality

                elif chunk.type == 1073741824:  # image data
                    raw = f.read(20)
                    image_data = e2e_binary.image_structure.parse(raw)
                    count = image_data.height * image_data.width
                    if count == 0:
                        break
                    if chunk.ind == 0:  # fundus data
                        raw_volume = np.frombuffer(f.read(count), dtype=np.uint8)
                        image = np.array(raw_volume).reshape(
                            image_data.height, image_data.width
                        )

                        image_string = "{}_{}_{}".format(
                            chunk.patient_db_id, chunk.study_id, chunk.series_id
                        )
                        if (
                            image_string in image_array_dict.keys()
                            and extract_scan_repeats
                        ):
                            is_in_keys = True
                            while is_in_keys:
                                image_string = image_string + "_"
                                if image_string not in image_array_dict.keys():
                                    is_in_keys = False

                        image_array_dict[image_string] = image

            # Read metadata to attach to FundusImageWithMetaData
            metadata = self.read_all_metadata()

            fundus_images = []
            for key, image in image_array_dict.items():
                fundus_images.append(
                    FundusImageWithMetaData(
                        image=image,
                        patient_id=self.patient_id,
                        image_id=key,
                        laterality=laterality_dict[key]
                        if key in laterality_dict.keys()
                        else None,
                        acquisition_date=self.acquisition_date,
                        metadata=metadata,
                        pixel_spacing=[scalex, scalex],
                    )
                )

        return fundus_images

    def read_all_metadata(self):
        """
        Reads all available metadata and returns a dictionary.

        The metadata is a raw dump of everything available.

        Returns:
            dictionary with all metadata.
        """

        def _convert_to_dict(container):
            """Converts a container object to a dictionary"""
            return dict(
                (name, getattr(container, name))
                for name in container
                if not name.startswith("_")
            )

        metadata = dict()
        metadata["image_data"] = []
        metadata["bscan_data"] = []
        metadata["patient_data"] = []
        metadata["laterality_data"] = []
        metadata["contour_data"] = []
        metadata["fundus_data"] = []
        metadata["device_data"] = []
        metadata["examined_structure"] = {}
        metadata["scan_pattern"] = {}
        metadata["enface_modality"] = {}
        metadata["oct_modality"] = {}
        metadata["localizer"] = []
        metadata["eye_data"] = []
        metadata["uid_data"] = []
        metadata["time_data"] = []
        metadata["additional_device_data"] = []

        with open(self.filepath, "rb") as f:
            # get all subdirectories
            chunk_stack = []

            for position in self.directory_stack:
                f.seek(position + self.byte_skip)
                raw = f.read(52)
                directory_chunk = e2e_binary.main_directory_structure.parse(raw)

                for ii in range(directory_chunk.num_entries):
                    raw = f.read(44)
                    chunk = e2e_binary.sub_directory_structure.parse(raw)
                    if chunk.start > chunk.pos:
                        chunk_stack.append([chunk.start, chunk.size])

            # traverse all chunks and extract slices
            for start, pos in chunk_stack:
                f.seek(start + self.byte_skip)
                raw = f.read(60)
                try:
                    # Heidelberg's updated anonymization seems to cause problems with
                    # some chunks. Observed problems include an empty raw and problems
                    # with undecodable bytes. For now, these chunks are skipped...
                    chunk = e2e_binary.chunk_structure.parse(raw)
                except Exception:
                    continue

                image_string = "{}_{}_{}".format(
                    chunk.patient_db_id, chunk.study_id, chunk.series_id
                )

                if chunk.type == 9:  # patient data
                    patient_data = E2E._parse_patient_data_from_file(f, chunk)
                    if patient_data is not None:
                        metadata["patient_data"].append(_convert_to_dict(patient_data))

                elif chunk.type == 10004:  # bscan metadata
                    raw = f.read(104)
                    bscan_metadata = e2e_binary.bscan_metadata.parse(raw)
                    metadata["bscan_data"].append(_convert_to_dict(bscan_metadata))

                elif chunk.type == 1073741824:  # fundus data
                    raw = f.read(20)
                    fundus_data = e2e_binary.image_structure.parse(raw)
                    metadata["fundus_data"].append(_convert_to_dict(fundus_data))

                elif chunk.type == 11:  # laterality data
                    raw = f.read(20)
                    laterality_data = e2e_binary.lat_structure.parse(raw)
                    metadata["laterality_data"].append(
                        _convert_to_dict(laterality_data)
                    )

                elif chunk.type == 10019:  # contour data
                    raw = f.read(16)
                    contour_data = e2e_binary.contour_structure.parse(raw)
                    metadata["contour_data"].append(_convert_to_dict(contour_data))

                elif chunk.type == 1073741824:  # image data
                    raw = f.read(20)
                    image_data = e2e_binary.image_structure.parse(raw)
                    metadata["image_data"].append(_convert_to_dict(image_data))

                elif chunk.type == 9001:  # device data ("Heidelberg Retina Angiograph")
                    raw = f.read(chunk.size)
                    device_data = e2e_binary.device_name.parse(raw)
                    metadata["device_data"].append(_convert_to_dict(device_data))

                elif chunk.type == 9005:  # examined structure ("Retina")
                    raw = f.read(chunk.size)
                    structure_data = e2e_binary.examined_structure.parse(raw)
                    if image_string not in metadata["examined_structure"]:
                        metadata["examined_structure"][
                            image_string
                        ] = structure_data.text[0]

                elif chunk.type == 9006:  # scan pattern
                    raw = f.read(chunk.size)
                    scan_pattern = e2e_binary.scan_pattern.parse(raw)
                    if image_string not in metadata["scan_pattern"]:
                        metadata["scan_pattern"][image_string] = scan_pattern.text[0]

                elif chunk.type == 9007:  # enface_modality (i.e. IR, FA, ICGA)
                    raw = f.read(chunk.size)
                    enface = e2e_binary.enface_modality.parse(raw)
                    if image_string not in metadata["enface_modality"]:
                        metadata["enface_modality"][image_string] = enface.text[1]

                elif chunk.type == 9008:
                    raw = f.read(chunk.size)
                    oct_modality = e2e_binary.oct_modality.parse(raw)
                    if image_string not in metadata["oct_modality"]:
                        metadata["oct_modality"][image_string] = oct_modality.text[0]

                elif chunk.type == 10025:
                    raw = f.read(chunk.size)
                    localizer = e2e_binary.localizer.parse(raw)
                    metadata["localizer"].append(_convert_to_dict(localizer))

                elif chunk.type == 7:  # eye data
                    raw = f.read(chunk.size)
                    eye_data = e2e_binary.eye_data.parse(raw)
                    metadata["eye_data"].append(_convert_to_dict(eye_data))

                elif chunk.type == 39:  # time zone, possibly timestamps
                    try:
                        raw = f.read(chunk.size)
                        time_data = e2e_binary.time_data.parse(raw)
                        metadata["time_data"].append(_convert_to_dict(time_data))
                    except StreamError:
                        pass

                elif chunk.type in [52, 54, 1000, 1001]:  # various UIDs
                    try:
                        raw = f.read(chunk.size)
                        uid_data = e2e_binary.uid_data.parse(raw)
                        metadata["uid_data"].append(
                            {chunk.type: _convert_to_dict(uid_data)}
                        )
                    except StreamError:
                        pass

                # Chunks 1005, 1006, and 1007 seem to contain strings of device data,
                # including some servicers and distributors and other entities,
                # but not always in the same order.
                elif chunk.type in [1005, 1006]:
                    raw = f.read(chunk.size)
                    metadata["additional_device_data"].append(
                        {chunk.type: raw.decode()}
                    )

                elif chunk.type == 1007:
                    raw = f.read(chunk.size)
                    unknown_data = e2e_binary.unknown_data.parse(raw)
                    metadata["additional_device_data"].append(
                        {chunk.type: _convert_to_dict(unknown_data)}
                    )

        return metadata

    def read_custom_float(self, bytes: str) -> float:
        """Implementation of bespoke float type used in .e2e files.

        Notes:
            Custom float is a floating point type with no sign, 6-bit exponent, and 10-bit mantissa.

        Args:
            bytes: the two bytes.

        Returns:
            float
        """
        # convert two bytes to 16-bit binary representation
        bits = bin(bytes[0])[2:].zfill(8)[::-1] + bin(bytes[1])[2:].zfill(8)[::-1]

        # get mantissa and exponent
        mantissa = bits[:10]
        exponent = bits[10:]

        # convert to decimal representations
        mantissa_sum = 1 + int(mantissa, 2) / self.power
        exponent_sum = int(exponent[::-1], 2) - 63
        decimal_value = mantissa_sum * pow(2, exponent_sum)
        return decimal_value

    def uint16_to_ufloat16(self, uint16: int) -> float:
        """Implementation of bespoke float type used in .e2e files.

        Notes:
            Custom float is a floating point type with no sign, 6-bit exponent, and 10-bit mantissa.

        Args:
            uint16

        Returns:
            float
        """
        bits = "{0:016b}".format(uint16)[::-1]
        # get mantissa and exponent
        mantissa = bits[:10]
        exponent = bits[10:]
        exponent = exponent[::-1]

        # convert to decimal representations
        mantissa_sum = 1 + int(mantissa, 2) / self.power
        exponent_sum = int(exponent, 2) - 63
        decimal_value = mantissa_sum * np.float_power(2, exponent_sum)
        return decimal_value

    def vol_intensity_transform(self, data: np.array) -> np.array:
        """Implementation of intensity transform used in .e2e files.

        Notes:
            Code thanks to @oli4, see discussion in https://github.com/marksgraham/OCT-Converter/issues/21#issuecomment-1057455183
        """
        selection_0 = data == np.finfo(np.float32).max
        selection_data = data <= 1

        new = np.log(data[selection_data] + 2.44e-04)
        new = (new + 8.3) / 8.285

        data[selection_data] = new
        data[selection_0] = 0
        data = np.clip(data, 0, 1)
        return data
