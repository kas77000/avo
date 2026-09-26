@echo off
cd /d "%~dp0"

python .\Historical\historical_ticks.py --venues "ASX-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "BSE-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "HKG-GEM" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "HKG-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "JKT-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "KLS-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "KOE-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "KSC-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "NSI-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "NZX-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "PHS-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SES-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SET-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SHA-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SHH-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SHZ-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SSC-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SZA-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "SZC-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "TAI-MAIN" --backfill 31 --chunk 30 --compress_venues
python .\Historical\historical_ticks.py --venues "TYO-MAIN" --backfill 31 --chunk 30 --compress_venues
