import os
import sys

# Tambahkan direktori root proyek ke sys.path agar modul utama (app.py) dapat di-import oleh runtime Vercel
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from app import app

# Vercel Python WSGI handler
# Variabel 'app' akan dieksekusi sebagai serverless function oleh @vercel/python
