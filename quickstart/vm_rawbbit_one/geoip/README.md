# GeoIP database

The one-VM quickstart defaults to `GEOIP_ENABLED=0` because the GeoIP database
is a binary data file and may have its own download/update lifecycle.

To enable GeoIP enrichment:

1. Put the database at:

   ```text
   quickstart/vm_rawbbit_one/geoip/dbip-country-lite.mmdb
   ```

2. Set in `.env`:

   ```env
   GEOIP_ENABLED=1
   GEOIP_MMDB_PATH=/var/lib/geoip/dbip-country-lite.mmdb
   ```

The Compose file mounts the local `geoip/` directory into the collector
container at `/var/lib/geoip`.
