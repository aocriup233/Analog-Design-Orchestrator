"""Local Python desktop UI for technology-independent lookup tables."""
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from .gmid import LookupTable


def main():
    root = tk.Tk()
    root.title('gm/Id Lookup — local data only / SI units')
    table = None
    path_label = ttk.Label(root, text='Choose a local normalized CSV table')
    path_label.grid(row=0, column=0, columnspan=2, sticky='w')
    fields = {}
    defaults = {'model': '', 'corner': 'tt', 'temp_c': '27', 'l': '',
                'vds': '', 'vsb': '0', 'vgs': '', 'gm_id': '', 'current': '', 'branch': ''}
    for i, (name, default) in enumerate(defaults.items(), 2):
        ttk.Label(root, text=name).grid(row=i, column=0, sticky='w')
        fields[name] = tk.StringVar(value=default)
        ttk.Entry(root, textvariable=fields[name], width=40).grid(row=i, column=1, sticky='ew')
    output = tk.Text(root, width=70, height=18)
    output.grid(row=15, column=0, columnspan=3)

    def load():
        nonlocal table
        path = filedialog.askopenfilename(filetypes=[('Normalized CSV', '*.csv')])
        if path:
            try:
                candidate = LookupTable(path)
                table = candidate
                path_label.configure(text='Local table loaded')
                key = next(iter(table.tables))
                for name, value in zip(('model', 'corner', 'temp_c'), key):
                    fields[name].set('' if name in table.missing_context else str(value))
                for name in table.missing_context:
                    fields[name].set('')
            except Exception as exc:
                messagebox.showerror('Load error', str(exc))

    def run(mode):
        try:
            if table is None:
                raise ValueError('Choose a table first')
            args = {n: fields[n].get() for n in ('model', 'corner')}
            args['l'] = float(fields['l'].get())
            args.update({n: float(fields[n].get()) if fields[n].get().strip() else None
                         for n in ('temp_c', 'vds', 'vsb')})
            if mode == 'query':
                args['vgs'] = float(fields['vgs'].get())
            else:
                args['branch'] = int(fields['branch'].get()) if fields['branch'].get() else None
                args['gm_id' if mode == 'size' else 'target'] = float(fields['gm_id'].get())
                if mode == 'size':
                    args['current'] = float(fields['current'].get())
            result = getattr(table, mode)(**args)
            output.delete('1.0', tk.END)
            output.insert(tk.END, json.dumps(result, indent=2, allow_nan=False))
        except Exception as exc:
            messagebox.showerror('Query error', str(exc))

    ttk.Button(root, text='Open local CSV', command=load).grid(row=1, column=0)
    for i, (label, mode) in enumerate((('Query bias', 'query'), ('Invert gm/Id', 'at_gm_id'), ('Size W', 'size'))):
        ttk.Button(root, text=label, command=lambda m=mode: run(m)).grid(row=14, column=i)
    root.mainloop()


if __name__ == '__main__':
    main()
