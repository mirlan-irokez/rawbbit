select
  app_id,
  event_id,
  count() as row_count
from {{ ref('rawbbit_events_load') }}
group by app_id, event_id
having row_count > 1
