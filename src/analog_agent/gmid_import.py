"""Import paired X/Y CSV exports with an explicit, private mapping manifest."""
import argparse
import csv
import json
import math
from pathlib import Path
from .gmid import REQUIRED


def convert(manifest_path, destination):
    """No technology assumptions: column indexes are zero based; scales yield SI.

    Each series supplies constant model/corner/temp/l/vds/vsb/w_ref and one
    source per primitive field. VGS vectors must match after declared scaling.
    """
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    rows = []
    for series in manifest['series']:
        constants = series['constants']
        fields, reference = {}, None
        for name, source in series['sources'].items():
            with (manifest_path.parent/source['path']).open(encoding='utf-8-sig', newline='') as stream:
                raw = list(csv.reader(stream))[source.get('skip_rows', 1):source.get('stop_row')]
            x = [float(r[source['x_column']])*source.get('x_scale', 1) for r in raw if r]
            y = [float(r[source['y_column']])*source.get('scale', 1) for r in raw if r]
            if source.get('magnitude', False):
                y = list(map(abs, y))
            if source.get('x_magnitude', False):
                x = list(map(abs, x))
            if reference is not None and (len(x) != len(reference) or any(
                    not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-15) for a, b in zip(x, reference))):
                raise ValueError('Source VGS vectors differ')
            reference, fields[name] = x, y
        if reference is None:
            raise ValueError('No series sources')
        for i, vgs in enumerate(reference):
            row = dict(constants, vgs=vgs, **{name: values[i] for name, values in fields.items()})
            # Old exports often contain ratios rather than primitive values.
            if 'id_w' in row:
                row.setdefault('id', row['id_w']*row['w_ref'])
            if 'gm_id' in row:
                row.setdefault('gm', row['gm_id']*row['id'])
            if 'gm_gds' in row:
                row.setdefault('gds', row['gm']/row['gm_gds'])
            if 'ft' in row:
                row.setdefault('cgg', row['gm']/(2*math.pi*row['ft']))
            if not set(REQUIRED) <= row.keys():
                raise ValueError('Mapping does not supply required primitive data')
            rows.append(row)
    if not rows:
        raise ValueError('Empty import')
    columns = list(REQUIRED) + sorted(set.intersection(*(set(r) for r in rows))-set(REQUIRED))
    # Validate in memory before writing; never overwrite source exports.
    destination = Path(destination)
    source_paths = {(manifest_path.parent/s['path']).resolve()
                    for series in manifest['series'] for s in series['sources'].values()}
    if destination.resolve() in source_paths or destination.resolve() == manifest_path.resolve():
        raise ValueError('Destination must differ from sources')
    with destination.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    try:
        from .gmid import LookupTable
        LookupTable(destination)
    except Exception:
        destination.unlink()
        raise
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(f'Imported {convert(args.manifest, args.destination)} points locally')


if __name__ == '__main__':
    main()
