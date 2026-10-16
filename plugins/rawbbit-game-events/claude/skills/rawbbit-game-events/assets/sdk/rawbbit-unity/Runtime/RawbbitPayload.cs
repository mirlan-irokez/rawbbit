using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Rawbbit.Unity
{
    internal static class RawbbitPayload
    {
        private const int MaximumJsonDepth = 32;
        private static readonly HashSet<string> AllowedEnvelopeFields = new HashSet<string>(StringComparer.Ordinal)
        {
            "event_id", "app_id", "environment", "event_name", "event_timestamp", "user", "device",
            "event_params", "user_properties", "traffic_source", "geo", "consent"
        };

        internal static RawbbitEventInput CloneInput(RawbbitEventInput input)
        {
            if (input == null) return null;
            return new RawbbitEventInput
            {
                EventName = input.EventName,
                EventId = input.EventId,
                EventTimestamp = input.EventTimestamp,
                UserId = input.UserId,
                UserPseudoId = input.UserPseudoId,
                Device = CloneMap(input.Device),
                EventParams = CloneMap(input.EventParams),
                UserProperties = CloneMap(input.UserProperties),
                TrafficSource = CloneMap(input.TrafficSource),
                Geo = CloneMap(input.Geo)
            };
        }

        internal static string BuildEventJson(
            RawbbitEventInput input,
            RawbbitConfiguration config,
            string installationId,
            string sessionId,
            string eventId,
            DateTimeOffset occurredAtUtc)
        {
            ValidateEventName(input == null ? null : input.EventName);
            if (String.IsNullOrWhiteSpace(eventId) || eventId.Length > 128)
                throw new ArgumentException("EventId must be a non-empty string of at most 128 characters.");

            JObject eventParams = ToObject(input.EventParams);
            AddDefault(eventParams, "product", config.Product);
            AddDefault(eventParams, "surface", config.Surface);
            AddDefault(eventParams, "source_type", config.SourceType);
            AddDefault(eventParams, "schema_version", config.SchemaVersion);

            JObject device = ToObject(config.DefaultDevice);
            foreach (JProperty property in ToObject(input.Device).Properties())
                device[property.Name] = property.Value.DeepClone();
            // Device platform is an SDK-owned distinction between Android/iOS
            // builds; an adapter value must not relabel the player.
            device["platform"] = config.Platform;

            string timestamp = FormatTimestamp(input.EventTimestamp, occurredAtUtc);
            string pseudoId = !String.IsNullOrWhiteSpace(input.UserPseudoId) ? input.UserPseudoId : installationId;

            var payload = new JObject
            {
                ["event_id"] = eventId,
                ["app_id"] = config.AppId,
                ["environment"] = config.Environment,
                ["event_name"] = input.EventName,
                ["event_timestamp"] = timestamp,
                ["user"] = new JObject
                {
                    ["user_id"] = input.UserId == null ? JValue.CreateNull() : new JValue(input.UserId),
                    ["user_pseudo_id"] = pseudoId,
                    ["session_id"] = sessionId
                },
                ["device"] = device,
                ["event_params"] = eventParams,
                ["user_properties"] = ToObject(input.UserProperties),
                ["traffic_source"] = ToObject(input.TrafficSource),
                ["geo"] = ToObject(input.Geo),
                ["consent"] = new JObject { ["analytics_storage"] = true }
            };

            ValidateToken(payload, 0);
            string json = payload.ToString(Formatting.None);
            if (!ValidateFrozenEvent(json, config.AppId, config.Environment, config.Platform, config.MaxRequestBytes))
                throw new ArgumentException("The event does not match the Collector envelope contract.");
            if (Encoding.UTF8.GetByteCount(json) + 13L > config.MaxRequestBytes)
                throw new ArgumentException("The event is too large to fit in a single Collector request.");
            return json;
        }

        internal static string BuildBatchBody(IList<RawbbitQueueRecord> records, RawbbitConfiguration config)
        {
            var builder = new StringBuilder("{\"events\":[");
            for (int i = 0; i < records.Count; i++)
            {
                if (!ValidateFrozenEvent(records[i].PayloadJson, config.AppId, config.Environment, config.Platform, config.MaxRequestBytes))
                    throw new ArgumentException("A frozen event does not match the configured Collector envelope.");
                if (i > 0) builder.Append(',');
                builder.Append(records[i].PayloadJson);
            }
            builder.Append("]}");
            string body = builder.ToString();
            if (records.Count > config.MaxEventsPerRequest || Encoding.UTF8.GetByteCount(body) > config.MaxRequestBytes)
                throw new ArgumentException("The batch exceeds configured Collector request limits.");
            return body;
        }

        internal static bool ValidateFrozenEvent(string json, string appId, string environment, string platform, long maximumRequestBytes)
        {
            try
            {
                JObject token = ParseToken(json) as JObject;
                if (token == null) return false;
                foreach (JProperty property in token.Properties())
                    if (!AllowedEnvelopeFields.Contains(property.Name)) return false;
                string[] required = { "event_id", "app_id", "event_name", "event_timestamp", "environment" };
                for (int i = 0; i < required.Length; i++)
                {
                    JToken value = token[required[i]];
                    if (value == null || value.Type != JTokenType.String || String.IsNullOrWhiteSpace((string)value)) return false;
                }
                Guid eventId;
                if (!Guid.TryParse((string)token["event_id"], out eventId)) return false;
                if (!String.Equals((string)token["app_id"], appId, StringComparison.Ordinal) ||
                    !String.Equals((string)token["environment"], environment, StringComparison.Ordinal)) return false;
                string eventName = (string)token["event_name"];
                if (eventName.Length > 128) return false;
                for (int i = 0; i < eventName.Length; i++) if (Char.IsControl(eventName[i])) return false;
                DateTimeOffset parsed;
                if (!DateTimeOffset.TryParseExact((string)token["event_timestamp"], "yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out parsed)) return false;
                JToken user = token["user"];
                JToken device = token["device"];
                JToken parameters = token["event_params"];
                JToken properties = token["user_properties"];
                JToken trafficSource = token["traffic_source"];
                JToken geo = token["geo"];
                JToken consent = token["consent"];
                if (user == null || user.Type != JTokenType.Object || device == null || device.Type != JTokenType.Object ||
                    parameters == null || parameters.Type != JTokenType.Object || properties == null || properties.Type != JTokenType.Object) return false;
                if (!ValidateOptionalStringFields((JObject)user, "user_id", "user_pseudo_id", "session_id")) return false;
                if (!ValidateOptionalStringFields((JObject)device, "platform", "app_version", "os_version", "device_model", "locale", "timezone")) return false;
                if (!String.Equals((string)device["platform"], platform, StringComparison.Ordinal)) return false;
                if (trafficSource == null || trafficSource.Type != JTokenType.Object || geo == null || geo.Type != JTokenType.Object ||
                    consent == null || consent.Type != JTokenType.Object || !ValidateOptionalBooleanFields((JObject)consent, "analytics_storage", "ads_storage", "personalization") ||
                    consent["analytics_storage"] == null || consent["analytics_storage"].Type != JTokenType.Boolean || !(bool)consent["analytics_storage"]) return false;
                if (!ValidateOptionalStringFields((JObject)trafficSource, "source", "medium", "campaign") ||
                    !ValidateOptionalStringFields((JObject)geo, "country", "country_code", "city", "region", "timezone")) return false;
                if (!OnlyKnownFields((JObject)user, "user_id", "user_pseudo_id", "session_id") ||
                    !OnlyKnownFields((JObject)device, "platform", "app_version", "os_version", "device_model", "locale", "timezone") ||
                    !OnlyKnownFields((JObject)trafficSource, "source", "medium", "campaign") ||
                    !OnlyKnownFields((JObject)geo, "country", "country_code", "city", "region", "timezone") ||
                    !OnlyKnownFields((JObject)consent, "analytics_storage", "ads_storage", "personalization")) return false;
                if (token.Property("received_at") != null || token.Property("ingest") != null) return false;
                ValidateToken(token, 0);
                return Encoding.UTF8.GetByteCount(json) + 13L <= maximumRequestBytes;
            }
            catch
            {
                return false;
            }
        }

        internal static long MeasureRecordBytes(RawbbitQueueRecord record)
        {
            // The cap accounts for frozen JSON plus a conservative reservation
            // for bounded handle/time/status metadata. Journal/snapshot framing
            // is separately bounded and documented as compaction overhead.
            return Encoding.UTF8.GetByteCount(record.PayloadJson ?? String.Empty) + 512L;
        }

        internal static long MeasureBatchBytes(IList<RawbbitQueueRecord> records)
        {
            long bytes = 13; // {"events":[ ... ]}
            for (int i = 0; i < records.Count; i++)
            {
                if (i > 0) bytes++;
                bytes += Encoding.UTF8.GetByteCount(records[i].PayloadJson ?? String.Empty);
            }
            return bytes;
        }

        internal static void ValidateInput(RawbbitEventInput input)
        {
            ValidateInput(input, 1024L * 1024L);
        }

        internal static void ValidateInput(RawbbitEventInput input, long maximumRequestBytes)
        {
            if (input == null) throw new ArgumentNullException("input");
            ValidateIdentityFields(input, maximumRequestBytes);
            long inputJsonBytes = 0;
            inputJsonBytes += MeasureObjectBytes(input.Device);
            inputJsonBytes += MeasureObjectBytes(input.EventParams);
            inputJsonBytes += MeasureObjectBytes(input.UserProperties);
            inputJsonBytes += MeasureObjectBytes(input.TrafficSource);
            inputJsonBytes += MeasureObjectBytes(input.Geo);
            inputJsonBytes += MeasureStringBytes(input.EventName) + MeasureStringBytes(input.UserId) +
                MeasureStringBytes(input.UserPseudoId) + MeasureStringBytes(input.EventTimestamp);
            if (inputJsonBytes > maximumRequestBytes)
                throw new ArgumentException("Event inputs exceed the configured request-size budget.");
        }

        internal static void ValidateIdentityFields(RawbbitEventInput input, long maximumRequestBytes)
        {
            if (input == null) throw new ArgumentNullException("input");
            ValidateEventName(input.EventName);
            long stringBytes = MeasureStringBytes(input.EventName) + MeasureStringBytes(input.UserId) +
                MeasureStringBytes(input.UserPseudoId) + MeasureStringBytes(input.EventTimestamp);
            if (stringBytes > maximumRequestBytes) throw new ArgumentException("Event identity fields exceed the configured request-size budget.");
            if (input.EventTimestamp != null && input.EventTimestamp.Length > 128)
                throw new ArgumentException("EventTimestamp is too long.");
            if (input.EventId != null)
            {
                Guid parsed;
                if (!Guid.TryParse(input.EventId, out parsed)) throw new ArgumentException("EventId must be a UUID.");
            }
        }

        private static long MeasureObjectBytes(IDictionary<string, object> values)
        {
            return values == null ? 2L : Encoding.UTF8.GetByteCount(ToObject(values).ToString(Formatting.None));
        }

        private static long MeasureStringBytes(string value)
        {
            return value == null ? 0L : Encoding.UTF8.GetByteCount(value);
        }

        internal static string FormatTimestampForSdk(string supplied, DateTimeOffset fallback)
        {
            return FormatTimestamp(supplied, fallback);
        }

        internal static string NormalizeEndpoint(string collectorUrl)
        {
            string value = collectorUrl.Trim().TrimEnd('/');
            if (value.EndsWith("/v1/events:batch", StringComparison.OrdinalIgnoreCase)) return value;
            return value + "/v1/events:batch";
        }

        private static string FormatTimestamp(string supplied, DateTimeOffset fallback)
        {
            DateTimeOffset value = fallback.ToUniversalTime();
            if (!String.IsNullOrWhiteSpace(supplied) && !DateTimeOffset.TryParse(supplied, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out value))
                throw new ArgumentException("EventTimestamp must be a valid ISO-8601 timestamp.");
            return value.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
        }

        private static void ValidateEventName(string name)
        {
            if (String.IsNullOrWhiteSpace(name) || name.Length > 128)
                throw new ArgumentException("EventName must be a non-empty string of at most 128 characters.");
            for (int i = 0; i < name.Length; i++)
                if (Char.IsControl(name[i])) throw new ArgumentException("EventName cannot contain control characters.");
        }

        private static JObject ToObject(IDictionary<string, object> values)
        {
            if (values == null) return new JObject();
            JToken token = ToJsonToken(values, 0, new HashSet<object>(ReferenceComparer.Instance));
            if (token.Type != JTokenType.Object) throw new ArgumentException("Event values must be string-keyed JSON objects.");
            ValidateToken(token, 0);
            return (JObject)token;
        }

        private static JToken ToJsonToken(object value, int depth, HashSet<object> ancestors)
        {
            if (depth > MaximumJsonDepth) throw new ArgumentException("Event JSON exceeds the maximum nesting depth.");
            if (value == null) return JValue.CreateNull();
            if (value is string) return new JValue((string)value);
            if (value is bool) return new JValue((bool)value);
            if (value is byte) return new JValue((byte)value);
            if (value is sbyte) return new JValue((sbyte)value);
            if (value is short) return new JValue((short)value);
            if (value is ushort) return new JValue((ushort)value);
            if (value is int) return new JValue((int)value);
            if (value is uint) return new JValue((uint)value);
            if (value is long) return new JValue((long)value);
            if (value is ulong) return new JValue((ulong)value);
            if (value is decimal) return new JValue((decimal)value);
            if (value is float)
            {
                float number = (float)value;
                if (Single.IsNaN(number) || Single.IsInfinity(number)) throw new ArgumentException("Non-finite JSON numbers are not supported.");
                return new JValue(number);
            }
            if (value is double)
            {
                double number = (double)value;
                if (Double.IsNaN(number) || Double.IsInfinity(number)) throw new ArgumentException("Non-finite JSON numbers are not supported.");
                return new JValue(number);
            }

            var genericDictionary = value as IDictionary<string, object>;
            if (genericDictionary != null)
            {
                EnterComposite(value, ancestors);
                try
                {
                    var result = new JObject();
                    foreach (KeyValuePair<string, object> pair in genericDictionary)
                    {
                        if (pair.Key == null) throw new ArgumentException("JSON object keys must be strings.");
                        result.Add(pair.Key, ToJsonToken(pair.Value, depth + 1, ancestors));
                    }
                    return result;
                }
                finally { ancestors.Remove(value); }
            }

            var dictionary = value as IDictionary;
            if (dictionary != null)
            {
                EnterComposite(value, ancestors);
                try
                {
                    var result = new JObject();
                    foreach (DictionaryEntry pair in dictionary)
                    {
                        string key = pair.Key as string;
                        if (key == null) throw new ArgumentException("JSON object keys must be strings.");
                        result.Add(key, ToJsonToken(pair.Value, depth + 1, ancestors));
                    }
                    return result;
                }
                finally { ancestors.Remove(value); }
            }

            var list = value as IList;
            if (list != null)
            {
                EnterComposite(value, ancestors);
                try
                {
                    var result = new JArray();
                    for (int i = 0; i < list.Count; i++) result.Add(ToJsonToken(list[i], depth + 1, ancestors));
                    return result;
                }
                finally { ancestors.Remove(value); }
            }

            throw new ArgumentException("Only JSON-compatible values are supported in event objects.");
        }

        private static void EnterComposite(object value, HashSet<object> ancestors)
        {
            if (!ancestors.Add(value)) throw new ArgumentException("Cyclic event objects are not supported.");
        }

        private static void AddDefault(JObject target, string name, string value)
        {
            if (value != null && target.Property(name) == null) target[name] = value;
        }

        private static void ValidateToken(JToken token, int depth)
        {
            if (token == null) return;
            if (depth > MaximumJsonDepth) throw new ArgumentException("Event JSON exceeds the maximum nesting depth.");
            if (token.Type == JTokenType.Object)
            {
                foreach (JProperty property in ((JObject)token).Properties()) ValidateToken(property.Value, depth + 1);
                return;
            }
            if (token.Type == JTokenType.Array)
            {
                foreach (JToken child in (JArray)token) ValidateToken(child, depth + 1);
                return;
            }
            if (token.Type == JTokenType.Float)
            {
                double number = (double)token;
                if (Double.IsNaN(number) || Double.IsInfinity(number)) throw new ArgumentException("Non-finite JSON numbers are not supported.");
                return;
            }
            if (token.Type == JTokenType.Integer || token.Type == JTokenType.String || token.Type == JTokenType.Boolean || token.Type == JTokenType.Null)
                return;
            throw new ArgumentException("Unsupported JSON token type.");
        }

        private static bool ValidateOptionalStringFields(JObject value, params string[] fields)
        {
            for (int i = 0; i < fields.Length; i++)
            {
                JToken field = value[fields[i]];
                if (field != null && field.Type != JTokenType.Null && field.Type != JTokenType.String) return false;
            }
            return true;
        }

        private static bool ValidateOptionalBooleanFields(JObject value, params string[] fields)
        {
            for (int i = 0; i < fields.Length; i++)
            {
                JToken field = value[fields[i]];
                if (field != null && field.Type != JTokenType.Null && field.Type != JTokenType.Boolean) return false;
            }
            return true;
        }

        private static bool OnlyKnownFields(JObject value, params string[] fields)
        {
            var known = new HashSet<string>(fields, StringComparer.Ordinal);
            foreach (JProperty property in value.Properties())
                if (!known.Contains(property.Name)) return false;
            return true;
        }

        private static JToken ParseToken(string json)
        {
            using (var stringReader = new StringReader(json))
            using (var reader = new JsonTextReader(stringReader) { DateParseHandling = DateParseHandling.None, FloatParseHandling = FloatParseHandling.Double })
            {
                JToken token = JToken.ReadFrom(reader, new JsonLoadSettings
                {
                    DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error
                });
                if (reader.Read()) throw new JsonReaderException("Trailing content after event JSON.");
                return token;
            }
        }

        internal static IDictionary<string, object> CloneMap(IDictionary<string, object> value)
        {
            if (value == null) return null;
            JObject token = ToObject(value);
            return (IDictionary<string, object>)FromToken(token);
        }

        private static object FromToken(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null) return null;
            if (token is JObject obj)
            {
                var result = new Dictionary<string, object>(StringComparer.Ordinal);
                foreach (JProperty property in obj.Properties()) result[property.Name] = FromToken(property.Value);
                return result;
            }
            if (token is JArray array)
            {
                var result = new List<object>();
                foreach (JToken child in array) result.Add(FromToken(child));
                return result;
            }
            return ((JValue)token).Value;
        }

        private sealed class ReferenceComparer : IEqualityComparer<object>
        {
            internal static readonly ReferenceComparer Instance = new ReferenceComparer();
            public bool Equals(object left, object right) { return Object.ReferenceEquals(left, right); }
            public int GetHashCode(object value) { return RuntimeHelpers.GetHashCode(value); }
        }
    }
}
