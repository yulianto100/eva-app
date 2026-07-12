from flask import Flask


def register_blueprints(_app: Flask) -> None:
    """Compatibility hook untuk migrasi bertahap ke Flask Blueprints.

    Route legacy masih berada di app.py agar perubahan infrastruktur tetap aman dan
    existing tests/imports tidak pecah. Modul ini disiapkan supaya migrasi berikutnya
    bisa memindahkan route per area tanpa mengubah entrypoint aplikasi.
    """
    return None
