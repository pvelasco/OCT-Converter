#!/usr/bin/env python3
"""
Script to extract study and acquisition date/time from Heidelberg E2E files.

Usage:
    python extract_e2e_dates.py <path_to_e2e_file>
"""

import sys
from pathlib import Path

from oct_converter.readers import E2E


def extract_dates(e2e_filepath):
    """
    Extract date and time information from a Heidelberg E2E file.

    Args:
        e2e_filepath: Path to the .e2e file

    Returns:
        dict containing extracted date/time information
    """
    print(f"\n{'=' * 60}")
    print(f"Reading E2E file: {e2e_filepath}")
    print(f"{'=' * 60}\n")

    # Initialize E2E reader
    e2e = E2E(e2e_filepath)

    # Read OCT volumes to populate metadata
    print("Extracting OCT volumes and metadata...")
    oct_volumes = e2e.read_oct_volume()

    print(f"Found {len(oct_volumes)} OCT volume(s)\n")

    # Extract dates from the E2E reader instance
    results = {
        "acquisition_date": e2e.acquisition_date,
        "patient_id": e2e.patient_id,
        "first_name": e2e.first_name,
        "surname": e2e.surname,
        "sex": e2e.sex,
        "birthdate": e2e.birthdate,
        "pixel_spacing": e2e.pixel_spacing,
    }

    # Display results
    print("=" * 60)
    print("EXTRACTED METADATA")
    print("=" * 60)

    print("\n--- Date/Time Information ---")
    if results["acquisition_date"]:
        print(f"Acquisition Date/Time: {results['acquisition_date']}")
        print(f" - Date: {results['acquisition_date'].date()}")
        print(f" - Time: {results['acquisition_date'].time()}")
        print(f" - ISO Format: {results['acquisition_date'].isoformat()}")
    else:
        print("Acquisition Date/Time: Not found")

    print("\n--- Patient Information ---")
    print(f"Patient ID: {results['patient_id']}")
    print(f"Name: {results['first_name']} {results['surname']}")
    print(f"Sex: {results['sex']}")
    print(f"Birth Date: {results['birthdate']}")

    print("\n--- Volume Information ---")
    for idx, volume in enumerate(oct_volumes, 1):
        print(f"\nVolume {idx}: ")
        print(f" - Volume ID: {volume.volume_id}")
        print(f" - Laterality: {volume.laterality}")
        print(f" - Number of slices: {volume.num_slices}")
        print(f" - Acquisition date: {volume.acquisition_date}")
        if volume.pixel_spacing:
            print(f" - Pixel spacing (x, y, z) mm: {volume.pixel_spacing}")
    print("\n" + "=" * 60)

    return results


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
        results = extract_dates(e2e_filepath)

        # Return success
        print("\n✓ Extraction completed successfully")
        return 0

    except Exception as e:
        print(f"\n✗ Error extracting dates: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
