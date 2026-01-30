#!/usr/bin/env python3
"""
Test script to check chunk size detection for patient data in E2E files.

Usage:
    python test_chunk_size.py <path_to_e2e_file>
"""

import sys
from pathlib import Path

# Add parent directory to path so oct_converter can be imported when running
# this script directly from any location (not just from repository root)
sys.path.insert(0, str(Path(__file__).parent.parent))

from oct_converter.readers.binary_structs import e2e_binary  # noqa: E402


def test_chunk_size(filepath):
    """Test chunk size detection for patient data."""
    with open(filepath, "rb") as f:
        # Check file type
        raw = f.read(21)
        if raw == b"E2EMultipleVolumeFile":
            byte_skip = 64
        else:
            byte_skip = 0

        print(f"byte_skip: {byte_skip}")

        # Read header and main directory
        f.seek(byte_skip)
        raw = f.read(36)
        header = e2e_binary.header_structure.parse(raw)

        raw = f.read(52)
        main_directory = e2e_binary.main_directory_structure.parse(raw)

        # Build directory stack
        directory_stack = []
        current = main_directory.current
        while current != 0:
            directory_stack.append(current)
            f.seek(current + byte_skip)
            raw = f.read(52)
            directory_chunk = e2e_binary.main_directory_structure.parse(raw)
            current = directory_chunk.prev

        # Build chunk stack
        chunk_stack = []
        for position in directory_stack:
            f.seek(position + byte_skip)
            raw = f.read(52)
            directory_chunk = e2e_binary.main_directory_structure.parse(raw)

            for ii in range(directory_chunk.num_entries):
                raw = f.read(44)
                try:
                    chunk = e2e_binary.sub_directory_structure.parse(raw)
                    if chunk.start > chunk.pos:
                        chunk_stack.append([chunk.start, chunk.size])
                except Exception:
                    continue

        # Find patient data chunks
        for start, size in chunk_stack:
            f.seek(start + byte_skip)
            raw_chunk = f.read(60)

            try:
                chunk = e2e_binary.chunk_structure.parse(raw_chunk)
            except Exception:
                continue

            if chunk.type == 9:
                print("\nFound patient data chunk:")
                print(f"  Position: {start}")
                print(f"  Chunk size: {chunk.size}")

                # Try parsing with both structures (same logic as E2E reader)
                pos = f.tell()
                raw = f.read(max(127, chunk.size))

                # Try 127-byte structure first
                print("\n  Attempting 127-byte structure (Int32un birthdate)...")
                try:
                    patient_data = e2e_binary.patient_id_structure.parse(raw[:127])
                    print("  ✓ Parsing: SUCCESS with 127-byte structure")
                    print(f"    first_name: '{patient_data.first_name.strip()}'")
                    print(f"    surname: '{patient_data.surname.strip()}'")
                    print(f"    birthdate: {patient_data.birthdate}")
                    print(f"    sex: '{patient_data.sex}'")
                    print(f"    patient_id: '{patient_data.patient_id.strip()}'")
                except Exception as e:
                    print(f"  ✗ 127-byte structure failed: {e}")

                    # Try 131-byte structure
                    print("\n  Attempting 131-byte structure (Float64l birthdate)...")
                    try:
                        f.seek(pos)
                        raw = f.read(131)
                        patient_data = e2e_binary.patient_id_structure_v2.parse(raw)
                        print("  ✓ Parsing: SUCCESS with 131-byte structure")
                        print(f"    first_name: '{patient_data.first_name.strip()}'")
                        print(f"    surname: '{patient_data.surname.strip()}'")
                        print(f"    birthdate: {patient_data.birthdate}")
                        print(f"    sex: '{patient_data.sex}'")
                        print(f"    patient_id: '{patient_data.patient_id.strip()}'")
                    except Exception as e2:
                        print(f"  ✗ 131-byte structure failed: {e2}")

                break


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("Error: Please provide path to E2E file")
        sys.exit(1)

    filepath = Path(sys.argv[1])

    if not filepath.exists():
        print(f"Error: File not found: {filepath}")
        sys.exit(1)

    test_chunk_size(filepath)
