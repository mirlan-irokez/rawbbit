using System;
using System.Collections.Generic;

namespace Rawbbit.Unity
{
    [Serializable]
    public sealed class RawbbitConfiguration
    {
        public string CollectorUrl;
        public string PreviousCollectorUrl;
        // Host assertion that this app/environment has no prior store at an
        // unspecified endpoint. Never use as an endpoint-migration shortcut.
        // Allows only legacy hash-named siblings, not unknown root entries.
        public bool AllowFreshStoreWithUnresolvedLegacyStores;
        public string ApiKey;
        public string AppId;
        public string Environment;
        public string Product;
        public string Surface;
        public string SourceType = "game_client";
        public string SchemaVersion = "1";
        public string Platform;
        public IDictionary<string, object> DefaultDevice;

        public long MaxQueueBytes = 10L * 1024L * 1024L;
        public int RetentionDays = 7;
        public int SessionTimeoutMinutes = 30;
        public int MaxEventsPerRequest = 500;
        public long MaxRequestBytes = 1024L * 1024L;
        public int RequestTimeoutSeconds = 10;
        public int MaxImmediateRetries = 2;
        public int RetryCooldownSeconds = 60;
        public long JournalCompactionBytes = 1024L * 1024L;
        public int MaxPendingTrackOperations = 64;
        public bool AllowInsecureHttpForDevelopment;

        internal void Validate()
        {
            if (String.IsNullOrWhiteSpace(CollectorUrl)) throw new ArgumentException("CollectorUrl is required.");
            Uri uri;
            if (!Uri.TryCreate(CollectorUrl, UriKind.Absolute, out uri) || (uri.Scheme != Uri.UriSchemeHttps && !(AllowInsecureHttpForDevelopment && uri.Scheme == Uri.UriSchemeHttp)))
                throw new ArgumentException("CollectorUrl must be an absolute HTTPS URL (HTTP is allowed only when explicitly enabled for development).");
            if (String.IsNullOrWhiteSpace(uri.Host)) throw new ArgumentException("CollectorUrl must include a host.");
            if (!String.IsNullOrEmpty(uri.UserInfo) || !String.IsNullOrEmpty(uri.Query) || !String.IsNullOrEmpty(uri.Fragment))
                throw new ArgumentException("CollectorUrl must not contain user information, a query, or a fragment.");
            if (PreviousCollectorUrl != null)
            {
                Uri previous;
                if (String.IsNullOrWhiteSpace(PreviousCollectorUrl) ||
                    !Uri.TryCreate(PreviousCollectorUrl, UriKind.Absolute, out previous) ||
                    (previous.Scheme != Uri.UriSchemeHttps && !(AllowInsecureHttpForDevelopment && previous.Scheme == Uri.UriSchemeHttp)) ||
                    String.IsNullOrWhiteSpace(previous.Host) || !String.IsNullOrEmpty(previous.UserInfo) ||
                    !String.IsNullOrEmpty(previous.Query) || !String.IsNullOrEmpty(previous.Fragment))
                    throw new ArgumentException("PreviousCollectorUrl must be a valid Collector URL.");
            }
            if (String.IsNullOrWhiteSpace(ApiKey) || ApiKey.Length > 4096 || ContainsNonHeaderCharacter(ApiKey)) throw new ArgumentException("ApiKey is required and must be a single header-safe value.");
            if (String.IsNullOrWhiteSpace(AppId) || AppId.Length > 256 || ContainsControl(AppId)) throw new ArgumentException("AppId is required and must be a valid value.");
            if (String.IsNullOrWhiteSpace(Environment) || Environment.Length > 64 || ContainsControl(Environment)) throw new ArgumentException("Environment is required and must be a valid value.");
            if (Platform != "android" && Platform != "ios" && Platform != "unity_editor" && Platform != "unity_other")
                throw new ArgumentException("Platform must be assigned by the Unity host.");
            if (MaxQueueBytes < 1024 || MaxQueueBytes > 100L * 1024L * 1024L) throw new ArgumentOutOfRangeException("MaxQueueBytes");
            if (RetentionDays < 1 || RetentionDays > 365) throw new ArgumentOutOfRangeException("RetentionDays");
            if (SessionTimeoutMinutes < 1 || SessionTimeoutMinutes > 1440) throw new ArgumentOutOfRangeException("SessionTimeoutMinutes");
            if (MaxEventsPerRequest < 1 || MaxEventsPerRequest > 500) throw new ArgumentOutOfRangeException("MaxEventsPerRequest", "The configured limit must be between 1 and the Collector default maximum of 500.");
            if (MaxRequestBytes < 512 || MaxRequestBytes > 1024L * 1024L) throw new ArgumentOutOfRangeException("MaxRequestBytes", "The configured limit must be between 512 bytes and the Collector default maximum of 1 MiB.");
            if (RequestTimeoutSeconds < 1 || RequestTimeoutSeconds > 120) throw new ArgumentOutOfRangeException("RequestTimeoutSeconds");
            if (MaxImmediateRetries < 0 || MaxImmediateRetries > 10) throw new ArgumentOutOfRangeException("MaxImmediateRetries");
            if (RetryCooldownSeconds < 1 || RetryCooldownSeconds > 3600) throw new ArgumentOutOfRangeException("RetryCooldownSeconds");
            if (JournalCompactionBytes < 64 * 1024 || JournalCompactionBytes > 8L * 1024L * 1024L) throw new ArgumentOutOfRangeException("JournalCompactionBytes");
            if (MaxPendingTrackOperations < 1 || MaxPendingTrackOperations > 64) throw new ArgumentOutOfRangeException("MaxPendingTrackOperations");
        }

        private static bool ContainsControl(string value)
        {
            for (int i = 0; i < value.Length; i++) if (Char.IsControl(value[i])) return true;
            return false;
        }

        private static bool ContainsNonHeaderCharacter(string value)
        {
            for (int i = 0; i < value.Length; i++)
                if (value[i] < 0x21 || value[i] > 0x7e) return true;
            return false;
        }
    }

    [Serializable]
    public sealed class RawbbitEventInput
    {
        public string EventName;
        public string EventId;
        public string EventTimestamp;
        public string UserId;
        public string UserPseudoId;
        public IDictionary<string, object> Device;
        public IDictionary<string, object> EventParams;
        public IDictionary<string, object> UserProperties;
        public IDictionary<string, object> TrafficSource;
        public IDictionary<string, object> Geo;
    }

    public sealed class RawbbitSessionContext
    {
        public string SessionId { get; internal set; }
        public int EventSequence { get; internal set; }
        public int SessionStartedEventSequence { get; internal set; }
        public int TriggeringEventSequence { get; internal set; }
        public bool IsNewSession { get; internal set; }
        public string EventTimestamp { get; internal set; }
    }

    public delegate RawbbitEventInput RawbbitSessionStartFactory(RawbbitSessionContext context, RawbbitEventInput triggeringEvent);
    public delegate void RawbbitEventContextMapper(RawbbitSessionContext context, RawbbitEventInput triggeringEvent);

    public enum RawbbitTrackStatus
    {
        DurablyStored,
        ConsentUnknown,
        ConsentWithdrawn,
        Busy,
        InvalidEvent,
        StorageFailure,
        DeliveryFaultPaused
    }

    public sealed class RawbbitTrackResult
    {
        public RawbbitTrackStatus Status { get; internal set; }
        public string EventId { get; internal set; }
        public string SafeDiagnosticCode { get; internal set; }
        public bool IsDurablyStored { get { return Status == RawbbitTrackStatus.DurablyStored; } }
    }

    public sealed class RawbbitQuarantinedRecordInfo
    {
        public string Handle { get; internal set; }
        public int HttpStatusCode { get; internal set; }
        public string FaultCode { get; internal set; }
        public DateTimeOffset EnqueuedAtUtc { get; internal set; }
    }

    public sealed class RawbbitQueueDiagnostics
    {
        public long QueueBytes { get; internal set; }
        public long QueueGeneration { get; internal set; }
        public int PendingCount { get; internal set; }
        public int QuarantinedCount { get; internal set; }
        public int LocallyRejectedCount { get; internal set; }
        public int ExpiredCount { get; internal set; }
        public int EvictedCount { get; internal set; }
        public int QuarantinedExpiredCount { get; internal set; }
        public int QuarantinedEvictedCount { get; internal set; }
        public int ExplicitlyDiscardedCount { get; internal set; }
        public string FaultCode { get; internal set; }
        public long FaultRevision { get; internal set; }
        public bool RecoveryProbeRequired { get; internal set; }
        public bool RecoveryProbeValidated { get; internal set; }
    }

    public interface IRawbbitClock
    {
        DateTimeOffset UtcNow { get; }
    }

    public interface IRawbbitIdentityProvider
    {
        string GetUserPseudoId();
        string GetUserId();
    }

    public interface IRawbbitSessionIdProvider
    {
        string CreateSessionId();
    }

    public sealed class RawbbitRecoveryResult
    {
        public bool Succeeded { get; internal set; }
        public string SafeDiagnosticCode { get; internal set; }
        public long FaultRevision { get; internal set; }
        public long QueueGeneration { get; internal set; }
    }

    internal sealed class RawbbitQueueRecord
    {
        public string Handle;
        public string EventId;
        public string PayloadJson;
        public long EnqueuedUtcTicks;
        public long QueueBytes;
        public bool Quarantined;
        public int HttpStatusCode;
        public string FaultCode;
        public long Sequence;
        public int OrderInTransaction;
    }

    internal sealed class RawbbitSessionState
    {
        public string SessionId;
        public long LastActivityUtcTicks;
        public int EventCounter;
    }

    internal sealed class RawbbitFaultState
    {
        public string FaultCode;
        public int HttpStatusCode;
        public long Revision;
        public long CreatedUtcTicks;
        public bool RequiresProbe;
        public bool ProbeValidated;
    }

    internal sealed class RawbbitRecoveryReceipt
    {
        public string OperationId;
        public string Operation;
        public string ReasonCode;
        public string PolicyVersion;
        public int AffectedCount;
        public long FaultRevision;
        public long CreatedUtcTicks;
    }

    internal sealed class RawbbitPersistentState
    {
        public int FormatVersion = 1;
        public long Sequence;
        public string InstallationId;
        public string CollectorEndpoint;
        public RawbbitSessionState Session = new RawbbitSessionState();
        public List<RawbbitQueueRecord> Records = new List<RawbbitQueueRecord>();
        public RawbbitFaultState Fault;
        public List<RawbbitRecoveryReceipt> Receipts = new List<RawbbitRecoveryReceipt>();
        public bool WithdrawalBarrier;
        public bool PurgePending;
        public long QueueGeneration;
        public int TotalExpiredCount;
        public int TotalEvictedCount;
        public int TotalQuarantinedExpiredCount;
        public int TotalQuarantinedEvictedCount;
        public int TotalExplicitlyDiscardedCount;
    }

    internal sealed class RawbbitStoreTransaction
    {
        public int FormatVersion = 1;
        public long Sequence;
        public List<RawbbitQueueRecord> Upsert = new List<RawbbitQueueRecord>();
        public List<string> RemoveHandles = new List<string>();
        public bool ClearRecords;
        public bool SetSession;
        public RawbbitSessionState Session;
        public bool SetInstallationId;
        public string InstallationId;
        public bool SetCollectorEndpoint;
        public string CollectorEndpoint;
        public bool SetFault;
        public RawbbitFaultState Fault;
        public bool SetWithdrawalBarrier;
        public bool WithdrawalBarrier;
        public bool SetPurgePending;
        public bool PurgePending;
        public bool IncrementQueueGeneration;
        public int ExpiredCount;
        public int EvictedCount;
        public int QuarantinedExpiredCount;
        public int QuarantinedEvictedCount;
        public int ExplicitlyDiscardedCount;
        public List<RawbbitRecoveryReceipt> AddReceipts = new List<RawbbitRecoveryReceipt>();
    }

    internal sealed class RawbbitSnapshotEnvelope
    {
        public int FormatVersion;
        public string Sha256;
        public string StateJson;
    }
}
