#!/usr/bin/env python3
"""
Debug script to troubleshoot E2E patient data parsing issues.

This script prints detailed information about how patient data (chunk type 9)
is being parsed from E2E files, including raw bytes and any exceptions.

Usage:
    python debug_e2e_patient_data.py <path_to_e2e_file>
"""

import sys
from datetime import datetime
from pathlib import Path


def debug_e2e_patient_data(e2e_filepath):
    """Debug E2E patient data parsing."""
    print("=" * 80)
    print("DEBUG: E2E Patient Data Extraction")
    print(f"File: {e2e_filepath}")
    print("=" * 80)

    # Import here to avoid issues if package not installed
    try:
        from oct_converter.readers.binary_structs import e2e_binary
    except ImportError as e:
        print(f"ERROR: Cannot import e2e_binary: {e}")
        print("Make sure you're running from the OCT-Converter directory")
        return

    with open(e2e_filepath, "rb") as f:
        # Check for multi-volume file header
        raw = f.read(21)
        if raw == b"E2EMultipleVolumeFile":
            byte_skip = 64
            print("\n✓ Multi-volume file detected (byte_skip = 64)")
        else:
            byte_skip = 0
            print("\n✓ Single volume file (byte_skip = 0)")

        # Read header
        f.seek(byte_skip)
        try:
            raw = f.read(36)
            header = e2e_binary.header_structure.parse(raw)
            print("\n✓ Header parsed successfully")
            print(f"  Magic: {header.magic1}")
            print(f"  Version: {header.version}")
        except Exception as e:
            print(f"\n✗ ERROR parsing header: {e}")
            import traceback

            traceback.print_exc()
            return

        # Read main directory
        try:
            raw = f.read(52)
            main_directory = e2e_binary.main_directory_structure.parse(raw)
            print("\n✓ Main directory parsed successfully")
            print(f"  Magic: {main_directory.magic2}")
            print(f"  Num entries: {main_directory.num_entries}")
            print(f"  Current: {main_directory.current}")
        except Exception as e:
            print(f"\n✗ ERROR parsing main directory: {e}")
            import traceback

            traceback.print_exc()
            return

        # Build directory stack (traverse linked list)
        print(f"\n{'=' * 80}")
        print("Building directory stack...")
        print("=" * 80)

        directory_stack = []
        current = main_directory.current
        while current != 0:
            directory_stack.append(current)
            f.seek(current + byte_skip)
            raw = f.read(52)
            try:
                directory_chunk = e2e_binary.main_directory_structure.parse(raw)
                current = directory_chunk.prev
            except Exception:
                break

        print(f"Found {len(directory_stack)} directories")

        # Build chunk stack from all directories
        print(f"\n{'=' * 80}")
        print("Scanning for chunks...")
        print("=" * 80)

        chunk_stack = []
        volume_dict = {}

        for position in directory_stack:
            f.seek(position + byte_skip)
            raw = f.read(52)
            directory_chunk = e2e_binary.main_directory_structure.parse(raw)

            for ii in range(directory_chunk.num_entries):
                raw = f.read(44)
                try:
                    chunk = e2e_binary.sub_directory_structure.parse(raw)
                    volume_string = (
                        f"{chunk.patient_db_id}_{chunk.study_id}_{chunk.series_id}"
                    )
                    if volume_string not in volume_dict:
                        volume_dict[volume_string] = chunk.slice_id / 2
                    elif chunk.slice_id / 2 > volume_dict[volume_string]:
                        volume_dict[volume_string] = chunk.slice_id / 2

                    if chunk.start > chunk.pos:
                        chunk_stack.append([chunk.start, chunk.size])
                except Exception:
                    continue

        print(f"Found {len(chunk_stack)} chunks to process")
        print(f"Found {len(volume_dict)} volumes")

        # Look for patient data chunks (type 9)
        patient_data_found = False
        patient_data_count = 0

        for start, size in chunk_stack:
            f.seek(start + byte_skip)
            raw_chunk = f.read(60)

            try:
                chunk = e2e_binary.chunk_structure.parse(raw_chunk)
            except Exception as e:
                continue

            if chunk.type == 9:  # patient data
                patient_data_count += 1
                patient_data_found = True

                print(f"\n{'=' * 80}")
                print(f"FOUND PATIENT DATA CHUNK #{patient_data_count}")
                print(f"{'=' * 80}")
                print(f"Chunk position: {start}")
                print(f"Chunk size: {chunk.size}")

                # Read the patient data structure (127 bytes)
                raw_patient = f.read(127)

                print(f"\nRaw patient data length: {len(raw_patient)} bytes")
                print("\nRaw bytes (hex):")
                print(raw_patient.hex())

                # Print in readable chunks
                print("\nRaw bytes (by field):")
                print(f"  first_name (0-31): {raw_patient[0:31].hex()}")
                print(f"  surname (31-82): {raw_patient[31:82].hex()}")
                print(f"  title (82-97): {raw_patient[82:97].hex()}")
                print(f"  birthdate (97-101): {raw_patient[97:101].hex()}")
                print(f"  sex (101-102): {raw_patient[101:102].hex()}")
                print(f"  patient_id (102-127): {raw_patient[102:127].hex()}")

                # Try to parse with construct
                print(f"\n{'=' * 80}")
                print("PARSING ATTEMPT")
                print("=" * 80)

                try:
                    patient_data = e2e_binary.patient_id_structure.parse(raw_patient)

                    print("✓ Parsing successful!")
                    print("\nParsed fields:")
                    print(f"  first_name: '{patient_data.first_name.strip()}'")
                    print(f"  surname: '{patient_data.surname.strip()}'")
                    print(f"  title: '{patient_data.title.strip()}'")
                    print(f"  birthdate: {patient_data.birthdate} (raw int)")
                    print(f"  sex: '{patient_data.sex}'")
                    print(f"  patient_id: '{patient_data.patient_id.strip()}'")

                    # Test the conversion function
                    print(f"\n{'=' * 80}")
                    print("BIRTHDATE CONVERSION")
                    print("=" * 80)

                    birthdate_value = patient_data.birthdate
                    print(f"Input value: {birthdate_value}")
                    print(f"Value type: {type(birthdate_value)}")
                    print(f"Value length (str): {len(str(birthdate_value))}")

                    # Check different conversion scenarios
                    if len(str(birthdate_value)) == 8:
                        print("→ Detected as YYYYMMDD format")
                        converted = str(birthdate_value)
                    elif birthdate_value > 100000:
                        ole_date = birthdate_value / 100000.0
                        print("→ Detected as fixed-point integer")
                        print(f"  OLE date: {ole_date}")
                        try:
                            unix_time = (ole_date - 25569) * 86400
                            print(f"  Unix time: {unix_time}")
                            converted = datetime.fromtimestamp(unix_time).strftime(
                                "%Y%m%d"
                            )
                        except Exception as e:
                            print(f"  ✗ Conversion failed: {e}")
                            converted = None
                    else:
                        print("→ Value too small for known formats")
                        converted = None

                    print(f"\nConverted birthdate: {converted}")

                except Exception as e:
                    print("✗ Parsing FAILED!")
                    print(f"Exception type: {type(e).__name__}")
                    print(f"Exception message: {e}")

                    import traceback

                    print("\nFull traceback:")
                    traceback.print_exc()

        if not patient_data_found:
            print(f"\n{'=' * 80}")
            print("WARNING: No patient data chunks (type 9) found!")
            print("=" * 80)

            # Show what chunk types were found
            print("\nChunk types found:")
            chunk_types = set()
            for start, size in chunk_stack:
                f.seek(start)
                raw_chunk = f.read(60)
                try:
                    chunk = e2e_binary.chunk_structure.parse(raw_chunk)
                    chunk_types.add(chunk.type)
                except Exception:
                    continue

            print(f"  {sorted(chunk_types)}")
        else:
            print(f"\n{'=' * 80}")
            print(f"SUMMARY: Found {patient_data_count} patient data chunk(s)")
            print("=" * 80)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Error: Please provide path to E2E file")
        sys.exit(1)

    e2e_filepath = Path(sys.argv[1])

    if not e2e_filepath.exists():
        print(f"Error: File not found: {e2e_filepath}")
        sys.exit(1)

    if e2e_filepath.suffix.lower() != ".e2e":
        print(f"Warning: File does not have .e2e extension: {e2e_filepath}")

    try:
        debug_e2e_patient_data(e2e_filepath)
        print("\n✓ Debug script completed")
        return 0

    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
