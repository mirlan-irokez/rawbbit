{% macro rawbbit_window() %}
  {% set default_lookback_hours = var('rawbbit_default_lookback_hours', 3) | int %}
  {% set default_end_dt = run_started_at.replace(minute=0, second=0, microsecond=0, tzinfo=None) %}
  {% set default_start_dt = default_end_dt - modules.datetime.timedelta(hours=default_lookback_hours) %}
  {% set default_start = default_start_dt.strftime('%Y-%m-%dT%H:%M:%SZ') %}
  {% set default_end = default_end_dt.strftime('%Y-%m-%dT%H:%M:%SZ') %}
  {% set window_start = var('rawbbit_window_start', default_start) %}
  {% set window_end = var('rawbbit_window_end', default_end) %}
  {% set timestamp_pattern = '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:00:00Z$' %}

  {% if not modules.re.match(timestamp_pattern, window_start) %}
    {{ exceptions.raise_compiler_error('rawbbit_window_start must be an hour-aligned UTC timestamp: ' ~ window_start) }}
  {% endif %}
  {% if not modules.re.match(timestamp_pattern, window_end) %}
    {{ exceptions.raise_compiler_error('rawbbit_window_end must be an hour-aligned UTC timestamp: ' ~ window_end) }}
  {% endif %}

  {% set start_dt = modules.datetime.datetime.strptime(window_start, '%Y-%m-%dT%H:%M:%SZ') %}
  {% set end_dt = modules.datetime.datetime.strptime(window_end, '%Y-%m-%dT%H:%M:%SZ') %}
  {% set window_seconds = (end_dt - start_dt).total_seconds() | int %}
  {% set max_window_hours = var('rawbbit_max_model_window_hours', 168) | int %}

  {% if window_seconds <= 0 %}
    {{ exceptions.raise_compiler_error('rawbbit_window_end must be after rawbbit_window_start') }}
  {% endif %}
  {% if window_seconds % 3600 != 0 %}
    {{ exceptions.raise_compiler_error('Rawbbit dbt windows must contain whole hours') }}
  {% endif %}
  {% if window_seconds > max_window_hours * 3600 %}
    {{ exceptions.raise_compiler_error('Rawbbit dbt window exceeds ' ~ max_window_hours ~ ' hours; use the chunked backfill wrapper') }}
  {% endif %}

  {{ return({
    'start': window_start,
    'end': window_end,
    'start_dt': start_dt,
    'end_dt': end_dt,
    'hour_count': window_seconds // 3600
  }) }}
{% endmacro %}

{% macro rawbbit_hour_paths() %}
  {% set window = rawbbit_window() %}
  {% set paths = [] %}
  {% for offset in range(window['hour_count']) %}
    {% set hour_dt = window['start_dt'] + modules.datetime.timedelta(hours=offset) %}
    {% do paths.append('app_id=*/event_date=' ~ hour_dt.strftime('%Y-%m-%d') ~ '/hour=' ~ hour_dt.strftime('%H') ~ '/*.parquet') %}
  {% endfor %}
  {{ return(paths) }}
{% endmacro %}

{% macro rawbbit_parquet_structure() %}
  {{ return(
    'event_id Nullable(String), app_id Nullable(String), environment Nullable(String), event_name Nullable(String), '
    ~ 'event_timestamp Nullable(String), received_at Nullable(String), user_id Nullable(String), '
    ~ 'user_pseudo_id Nullable(String), session_id Nullable(String), platform Nullable(String), '
    ~ 'app_version Nullable(String), os_version Nullable(String), device_model Nullable(String), '
    ~ 'locale Nullable(String), timezone Nullable(String), event_params_json Nullable(String), '
    ~ 'user_properties_json Nullable(String), traffic_source_json Nullable(String), geo_json Nullable(String), '
    ~ 'consent_json Nullable(String), ingest_request_id Nullable(String), ingest_user_agent Nullable(String), '
    ~ 'ingest_ip_hash Nullable(String), nats_stream Nullable(String), nats_sequence Nullable(Int64)'
  ) }}
{% endmacro %}
