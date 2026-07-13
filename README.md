# EVA - Engineering Vector Automation

[![CI](https://github.com/Dwi-Yulianto/eva-converter/actions/workflows/ci.yml/badge.svg)](https://github.com/Dwi-Yulianto/eva-converter/actions/workflows/ci.yml)

Web app untuk mengunggah, memvalidasi, preview mapping, lalu mengekspor data geospasial menjadi output CAD, BOQ, ASTRI, dan HPDB.

## Fitur

- Upload satu atau banyak file dengan drag & drop.
- Validasi ekstensi `.osm`, `.pbf`, `.osm.pbf`, `.kml`, dan `.kmz`.
- Validasi ukuran file maksimal 500MB.
- Konversi OSM/PBF/KML/KMZ ke DXF memakai `osmium`, `ezdxf`, `pyproj`, dan parser KML/KMZ bawaan.
- Banyak file dalam satu job digabung menjadi satu preview dan satu file DXF.
- Layer `ROAD` diekspor sebagai boundary koridor jalan otomatis hasil buffer + union, bukan centerline.
- Output `ROAD` memakai warna abu-abu dan memfilter noise seperti driveway, parking aisle, footway, path, steps, serta service road pendek.
- Input OSM/PBF otomatis hanya dikonversi sebagai `ROAD`; layer OSM lain tidak diekspor dari aplikasi.
- Projection default dari EPSG:4326 ke EPSG:3857.
- Layer utama output aplikasi: `ROAD` dan `KML`.
- Hasil convert otomatis di-scale dan dipindah ke kotak drawing frame kanan pada DXF output.
- Status konversi dengan polling AJAX.
- Statistik utama untuk `ROAD` dan `KML`.
- EVA Assistant floating chat untuk bantuan convert, troubleshooting, knowledge dokumen MR Design, dan guided tour UI.
- Pembacaan folder standar `.kml/.kmz` di browser, sehingga user bisa checklist folder standar yang ditemukan dan ingin dikonversi.
- Icon point KML/KMZ dikonversi ke simbol vektor DXF yang mendekati icon Google Earth seperti circle, triangle, target/crosshair, pushpin, dan home/garden/business.
- Preview GeoJSON di Leaflet.
- Download file DXF hasil konversi.
- Background thread untuk proses convert.
- Auto cleanup file upload/output lama.
- Logging proses.
- Siap berjalan di Windows, Linux, dan Docker.

## Install Lokal

Pastikan memakai Python 3.11 atau lebih baru.

```bash
pip install -r requirements.txt
```

## Menjalankan Lokal

```bash
python app.py
```

Buka:

```text
http://localhost:5000
```

Catatan penting:
- Default `DXF_CONVERSION_ENABLED=false`, jadi output default backend adalah `boq`, bukan `dxf`; aktifkan env ini kalau ingin DXF jadi default.
- Fitur HPDB/KMZ standardization butuh source coding FDT yang valid dari database atau spreadsheet (`FDT_CODING_SPREADSHEET_URL` / `FDT_CODING_SPREADSHEET_PATH`).
- Template output (`.dxf`, `.xlsx`, `.json`) wajib tersedia dan cocok dengan network/scope yang dipakai.

Di Windows tanpa akses admin, gunakan virtual environment lokal:

```bat
install_windows_no_admin.bat
run_windows.bat
```

## Tests

Default test runs skip long conversion tests marked `slow`, so `pytest tests/` stays useful for fast iteration. Run the slow slice with `pytest -m slow tests/`, or override the marker filter with `pytest -m "" tests/` to run everything.

## Cara Pakai

1. Buka halaman web.
2. Drag & drop satu atau beberapa file `.osm`, `.pbf`, `.osm.pbf`, `.kml`, atau `.kmz`.
3. Jika ada file KML/KMZ, pilih folder standar yang terdeteksi dan ingin dikonversi.
4. Klik tombol `Generate Output`.
5. Tunggu progress sampai selesai.
6. Lihat statistik `ROAD` dan `KML`, lalu preview peta.
7. Klik `Download DXF`.

## EVA Assistant

EVA Assistant memakai endpoint backend `POST /api/ai-chat`, jadi API key Xiaomi MiMo tidak pernah dikirim ke browser. Set environment berikut sebelum fitur chat dipakai:

```bash
XIAOMI_MIMO_API_KEY=isi_api_key
XIAOMI_MIMO_BASE_URL=https://provider.example/v1
XIAOMI_MIMO_MODEL=mimo-v2.5-pro
KNOWLEDGE_DIR=knowledge
KNOWLEDGE_DB_PATH=data/knowledge_index.db
```

Kalau `XIAOMI_MIMO_*` kosong, backend fallback ke `OPENAI_API_KEY`, `OPENAI_BASE_URL`, dan `OPENAI_MODEL` yang dipakai bot WhatsApp.

Knowledge base membaca file `.pdf`, `.pptx`, `.docx`, `.txt`, dan `.md` dari folder `knowledge/`.

```bash
curl -X POST http://localhost:5000/api/knowledge/reindex
curl http://localhost:5000/api/knowledge/status
```

Jika `REQUIRE_ADMIN_API_TOKEN=true`, endpoint reindex wajib memakai header `X-Admin-Token`. Endpoint chat dibatasi sekitar 10 request per menit per IP.

## RFS Incentive

Dashboard RFS menghitung estimasi insentif per area dari summary bulanan:

```text
Incentive IDR = Base Incentive x Difficulty x YTD Progress x Ach Kota x Payout Factor
```

Default `RFS_BASE_INCENTIVE_IDR=2000000`. Difficulty per area bisa diubah lewat `RFS_DIFFICULTY_BY_REGION`, contoh `Bekasi:1.25,Jakarta Selatan:1.25,Sukabumi:1.0`. Jika target bulanan/YTD belum ada, kolom insentif tetap muncul dengan status `NO TARGET` atau `NO YTD TARGET`.

## Standar Folder KML/KMZ

File `.kml` atau `.kmz` tidak wajib memiliki semua folder berikut. Converter hanya memproses folder yang namanya cocok persis dengan standar di bawah; folder yang tidak ada atau typo akan diabaikan.

```text
BOUNDARY FAT
FAT
HP COVER
HP UNCOVER
EXISTING POLE EMR 7-2.5
EXISTING POLE EMR 7-3
EXISTING POLE EMR 7-4
EXISTING POLE EMR 9-4
EXISTING POLE PARTNER 7-4
EXISTING POLE PARTNER 9-4
NEW POLE 7-2.5
NEW POLE 7-3
NEW POLE 7-4
NEW POLE 9-4
DISTRIBUTION CABLE
SLACK HANGER
SLING WIRE
```

Checklist folder di UI hanya menampilkan folder standar yang terdeteksi memiliki isi. Jika upload berisi banyak KML/KMZ, pilihan folder berlaku untuk semua file dalam batch, lalu hasil akhirnya tetap digabung menjadi satu DXF.

## Docker

```bash
docker compose up --build
```

Aplikasi akan berjalan di:

```text
http://localhost:5000
```

Docker Compose menjalankan tiga service: `web`, `worker`, dan `redis`. Status job, progress, dan log disimpan di Redis dengan TTL, sementara proses konversi berat dikerjakan oleh Celery worker. Jika `REDIS_URL` tidak diset, aplikasi lokal otomatis fallback ke memory job store dan background thread.

Sebelum deploy, pastikan:
- `SECRET_KEY` tidak memakai default `change-me-in-production`.
- file secret atau env untuk `SECRET_KEY` tersedia bila memakai Docker secrets.
- template DXF/BOQ/HPDB dan file default HPDB ada di path yang dikonfigurasi.
- source coding FDT tersedia lewat database atau spreadsheet URL/path.

## Struktur Folder

```text
eva-converter/
├── app.py
├── converter.py
├── config.py
├── jobs.py
├── job_queue.py
├── tasks.py
├── cleanup.py
├── metrics.py
├── alerts.py
├── hpdb.py
├── kmz_standardizer.py
├── fdt_coding_db.py
├── openapi.yaml
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
├── README.md
├── config/
├── data/
├── dxf_templates/
├── docs/
├── frontend/
├── uploads/
├── outputs/
├── templates/
├── static/
└── tests/
```

`frontend/` di repo ini bukan UI utama yang dibuild terpisah; UI utama tetap dirender dari Flask `templates/` + `static/`.

## Menjalankan Test

```bash
pytest
```

Untuk development/test lengkap:

```bash
pip install -r requirements-dev.txt
```

Runtime production cukup memakai `requirements.txt`; dependency test dipisah di `requirements-dev.txt`.

## Konfigurasi Environment

- `MAX_UPLOAD_MB`: batas upload dalam MB, default `500`.
- `MAX_BATCH_UPLOAD_MB`: batas total upload satu batch dalam MB, default `5000`.
- `CLEANUP_MAX_AGE_HOURS`: umur maksimal file upload/output sebelum dihapus, default `24`.
- `JOB_MAX_AGE_HOURS`: umur maksimal status job selesai/error, default `24`.
- `REDIS_URL`: URL Redis untuk persistent job store, log, Celery broker, dan result backend.
- `CONVERSION_QUEUE_ENABLED`: `true`, `false`, atau `auto`; default `auto` aktif saat Redis tersedia.
- `CONVERSION_MAX_CONCURRENT_JOBS`: batas job konversi berat yang boleh berjalan bersamaan, default `1`.
- `JOB_STORE_TTL_SECONDS`: TTL status job di Redis, default mengikuti `JOB_MAX_AGE_HOURS`.
- `JOB_SLOT_TTL_SECONDS`: TTL slot konversi untuk recovery jika worker mati, default `1800`.
- `CLEANUP_INTERVAL_SECONDS`: interval cleanup, default `3600`.
- `PREVIEW_FEATURE_LIMIT`: batas jumlah feature GeoJSON preview, default `2500`.
- `HOST`: host Flask lokal, default `0.0.0.0`.
- `PORT`: port Flask lokal, default `5000`.
- `LOG_LEVEL`: level logging, default `INFO`.
- `DXF_CONVERSION_ENABLED`: aktifkan output DXF sebagai target yang tersedia/default backend; default `false`.
- `FDT_CODING_SPREADSHEET_URL`: URL spreadsheet coding FDT untuk standardizer jika tidak memakai DB lokal.
- `FDT_CODING_SPREADSHEET_PATH`: path spreadsheet coding FDT lokal/cached.
- `FDT_CODING_DB_PATH`: path database coding FDT lokal.
- `HPDB_AUTO_GEOCODE_ENABLED`: aktif/nonaktif auto geocode HPDB, default `true`.

## Catatan Batasan

- Tanpa Redis, status job fallback ke memory dan akan hilang saat aplikasi restart.
- Endpoint `/readyz` sekarang memvalidasi secret, Redis, template DXF/BOQ/HPDB, default HPDB, source coding FDT, dan write access untuk direktori output utama.
- Endpoint `/health` ikut melaporkan hasil readiness agar dependency yang gagal lebih mudah didiagnosis.
- Multi-worker Gunicorn aman saat Redis aktif karena status job dibaca dari store bersama.
- File besar tetap bergantung pada RAM, CPU, dan performa disk server.
- Upload multi-file divalidasi per file dan total batch.
- File KML/KMZ tidak wajib memiliki semua folder standar. Nama folder yang ingin diproses harus cocok persis dengan standar; folder typo akan diabaikan.
- Placemark di subfolder standar, misalnya `HP COVER/A01`, tetap dihitung dan dikonversi sebagai bagian dari folder standar terdekat.
- File KML/KMZ ditulis ke layer `KML`; geometri yang didukung: `Point`, `LineString`, `Polygon`, dan placemark yang berisi banyak geometri.
- Template DWG asli tidak dibaca langsung oleh `ezdxf`; aplikasi membuat frame DXF standar dan menempatkan hasil convert ke area drawing frame kanan.
- Jika source coding FDT tidak siap atau gagal dibaca, standardizer tetap jalan tetapi hasil tertentu akan memakai placeholder `XXX` dan warning eksplisit.
- Preview GeoJSON dibatasi agar browser tetap responsif.
- Projection default adalah EPSG:3857. Fungsi projection sudah dipisah agar mudah diganti ke UTM.
- Boundary jalan dihitung otomatis setelah projection. Urutan estimasi lebar: tag `width`/`est_width`, lalu `lanes`, lalu fallback berdasarkan tipe `highway`. Semua koridor `ROAD` di-merge dengan Shapely, disederhanakan tipis, dan difilter dari jalan minor/noise sebelum ditulis ke DXF agar garis internal di persimpangan tidak ikut keluar.
- Aplikasi belum memakai autentikasi dan belum membatasi jumlah job paralel per user.

## Rencana Pengembangan

- Penyimpanan file upload/output ke object storage seperti S3.
- Pilihan projection UTM per zona.
- Filter layer sebelum export.
- Estimasi progress berbasis ukuran file atau pre-scan.
- Preview lebih besar memakai vector tile atau simplification.
- Rate limit upload dan autentikasi user.

 #   w e b h o o k   t e s t 