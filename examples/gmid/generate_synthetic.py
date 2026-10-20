"""Reproduce synthetic.csv from arithmetic; no simulator or PDK input."""
import csv
from pathlib import Path

columns = 'model corner temp_c l vds vsb vgs id gm gds cgg w_ref'.split()
with Path(__file__).with_name('synthetic.csv').open('w', encoding='utf-8', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for length in (1e-6, 2e-6):
        for vgs in (0.2, 0.4, 0.6):
            current = vgs * 1e-4
            writer.writerow(dict(model='synthetic', corner='tt', temp_c=27,
                l=length, vds=1, vsb=0, vgs=vgs, id=current,
                gm=current*(30-25*vgs), gds=current/10, cgg=1e-12, w_ref=1e-6))
