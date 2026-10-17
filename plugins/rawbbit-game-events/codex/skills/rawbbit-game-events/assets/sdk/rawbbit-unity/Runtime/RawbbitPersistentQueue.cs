using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Rawbbit.Unity
{
    internal interface IRawbbitQueueStore
    {
        string InstallationId { get; }
        bool IsStorageFaulted { get; }
        RawbbitSessionState ReadSession();
        RawbbitFaultState ReadFault();
        RawbbitQueueDiagnostics ReadDiagnostics();
        int PruneExpired(DateTimeOffset now);
        RawbbitEnqueueOutcome Enqueue(IList<RawbbitQueueRecord> records, RawbbitSessionState session, DateTimeOffset now);
        IList<RawbbitQueueRecord> GetPendingRecords(int maximumEvents, long maximumBytes, DateTimeOffset now);
        RawbbitQueueRecord GetOldestPendingRecord(DateTimeOffset now);
        bool TryDispatch(RawbbitPreparedBatch batch, Func<bool> startRequest);
        void Acknowledge(IList<string> handles);
        RawbbitQueueRecord GetRecoveryProbeRecord(DateTimeOffset now);
        void MarkRecoveryProbeValidated(long expectedRevision, long expectedQueueGeneration, string handle, DateTimeOffset now);
        void Quarantine(IList<string> handles, int statusCode, string faultCode, DateTimeOffset now, bool requiresProbe);
        void LatchFault(string faultCode, int statusCode, DateTimeOffset now, bool requiresProbe);
        IList<RawbbitQuarantinedRecordInfo> ListQuarantined();
        RawbbitRecoveryResult DiscardQuarantined(IList<string> handles, long expectedRevision, long expectedQueueGeneration, string reasonCode, string policyVersion, DateTimeOffset now);
        RawbbitRecoveryResult RetryQuarantined(IList<string> handles, long expectedRevision, long expectedQueueGeneration, string reasonCode, string policyVersion, DateTimeOffset now);
        RawbbitRecoveryResult ValidateAndResume(long expectedRevision, long expectedQueueGeneration, string reasonCode, string policyVersion, DateTimeOffset now);
        int WithdrawAndPurge(DateTimeOffset now);
        void GrantConsent();
    }

    internal sealed class RawbbitPersistentQueue : IRawbbitQueueStore
    {
        private const int MaximumJournalFrameBytes = 6 * 1024 * 1024;
        private const int MaximumReceipts = 64;
        private readonly object _sync = new object();
        private string _root;
        private readonly long _maxQueueBytes;
        private readonly long _journalCompactionBytes;
        private readonly int _retentionDays;
        private readonly IRawbbitClock _clock;
        private readonly string _appId;
        private readonly string _environment;
        private readonly string _platform;
        private readonly long _maxRequestBytes;
        private readonly JsonSerializerSettings _jsonSettings = new JsonSerializerSettings
        {
            TypeNameHandling = TypeNameHandling.None,
            ReferenceLoopHandling = ReferenceLoopHandling.Error,
            NullValueHandling = NullValueHandling.Include,
            Formatting = Formatting.None
        };
        private RawbbitPersistentState _state;
        private string _activeJournal;
        private bool _storageFaulted;
        private string _storageFaultCode;

        internal RawbbitPersistentQueue(string persistentRoot, RawbbitConfiguration configuration, IRawbbitClock clock)
        {
            configuration.Validate();
            _maxQueueBytes = configuration.MaxQueueBytes;
            _journalCompactionBytes = configuration.JournalCompactionBytes;
            _retentionDays = configuration.RetentionDays;
            _clock = clock ?? new SystemRawbbitClock();
            _appId = configuration.AppId;
            _environment = configuration.Environment;
            _platform = configuration.Platform;
            _maxRequestBytes = configuration.MaxRequestBytes;
            string endpoint = RawbbitPayload.NormalizeEndpoint(configuration.CollectorUrl);
            string previousEndpoint = configuration.PreviousCollectorUrl == null ? null : RawbbitPayload.NormalizeEndpoint(configuration.PreviousCollectorUrl);
            string identity = configuration.AppId + "\n" + configuration.Environment;
            string stableRoot = Path.Combine(persistentRoot, "Rawbbit", "store-v2-" + Hash(Encoding.UTF8.GetBytes(identity)).Substring(0, 32));
            string currentLegacyRoot = LegacyRoot(persistentRoot, endpoint, configuration);
            string previousLegacyRoot = previousEndpoint == null ? null : LegacyRoot(persistentRoot, previousEndpoint, configuration);
            var legacyRoots = new HashSet<string>(StringComparer.Ordinal);
            if (Directory.Exists(currentLegacyRoot)) legacyRoots.Add(currentLegacyRoot);
            if (previousLegacyRoot != null && Directory.Exists(previousLegacyRoot)) legacyRoots.Add(previousLegacyRoot);
            if (legacyRoots.Count > 1 || (Directory.Exists(stableRoot) && legacyRoots.Count > 0))
                throw new InvalidDataException("Conflicting Rawbbit stores require explicit operator reconciliation; no stores were merged.");
            string legacyRoot = legacyRoots.SingleOrDefault();
            string storesRoot = Path.Combine(persistentRoot, "Rawbbit");
            if (!Directory.Exists(stableRoot) && legacyRoot == null && Directory.Exists(storesRoot))
            {
                string[] siblings = Directory.GetDirectories(storesRoot);
                if (Directory.GetFiles(storesRoot).Length > 0 ||
                    siblings.Any(path => !HasStoreHashName(path, "store-v2-") && !HasStoreHashName(path, "")))
                    throw new InvalidDataException("Unrecognized Rawbbit root entries require operator reconciliation; fresh-store authorization does not apply.");
                if (!configuration.AllowFreshStoreWithUnresolvedLegacyStores && siblings.Any(path => HasStoreHashName(path, "")))
                    throw new InvalidDataException("Unresolved legacy stores require PreviousCollectorUrl, explicit fresh-store authorization, or operator reconciliation.");
            }
            _root = legacyRoot ?? stableRoot;
            Directory.CreateDirectory(_root);
            LoadOrInitialize();
            lock (_sync)
            {
                string sourceEndpoint = legacyRoot == null ? null : (legacyRoot == currentLegacyRoot ? endpoint : previousEndpoint);
                BindEndpoint(endpoint, previousEndpoint, sourceEndpoint);
                if (legacyRoot != null)
                {
                    // Bind durably before the same-parent rename. A restart on
                    // either side of the rename retains the endpoint binding.
                    Directory.Move(legacyRoot, stableRoot);
                    _root = stableRoot;
                    _activeJournal = Path.Combine(_root, Path.GetFileName(_activeJournal));
                }
                PruneExpired(_clock.UtcNow);
                EnsureInstallationId();
            }
        }

        private static string LegacyRoot(string persistentRoot, string endpoint, RawbbitConfiguration configuration)
        {
            string identity = endpoint + "\n" + configuration.AppId + "\n" + configuration.Environment;
            return Path.Combine(persistentRoot, "Rawbbit", Hash(Encoding.UTF8.GetBytes(identity)).Substring(0, 32));
        }

        private static bool HasStoreHashName(string path, string prefix)
        {
            string name = Path.GetFileName(path);
            if (name.Length != prefix.Length + 32 || !name.StartsWith(prefix, StringComparison.Ordinal)) return false;
            for (int i = prefix.Length; i < name.Length; i++)
            {
                char character = name[i];
                if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f'))) return false;
            }
            return true;
        }

        private void BindEndpoint(string endpoint, string previousEndpoint, string sourceEndpoint)
        {
            string existing = _state.CollectorEndpoint ?? sourceEndpoint;
            if (existing != null && existing != endpoint && existing != previousEndpoint)
                throw new InvalidDataException("Endpoint change requires PreviousCollectorUrl matching the stored endpoint.");
            if (_state.CollectorEndpoint == endpoint) return;
            bool endpointChanged = existing != null && existing != endpoint;
            RawbbitFaultState resetFault = endpointChanged && _state.Fault != null && _state.Fault.RequiresProbe
                ? Clone(_state.Fault) : null;
            if (resetFault != null)
            {
                resetFault.ProbeValidated = false;
                resetFault.Revision = _state.Sequence + 1;
            }
            Commit(new RawbbitStoreTransaction
            {
                SetCollectorEndpoint = true,
                CollectorEndpoint = endpoint,
                SetFault = resetFault != null,
                Fault = resetFault,
                IncrementQueueGeneration = true,
                AddReceipts = endpointChanged
                    ? new List<RawbbitRecoveryReceipt> { NewReceipt("endpoint_migration", "host_authorized", "sdk_v1", 0, _state.Fault == null ? 0 : _state.Fault.Revision, _clock.UtcNow) }
                    : new List<RawbbitRecoveryReceipt>()
            });
        }

        public string InstallationId
        {
            get { lock (_sync) return _state.InstallationId; }
        }

        public bool IsStorageFaulted
        {
            get { lock (_sync) return _storageFaulted; }
        }

        internal string StorageFaultCode
        {
            get { lock (_sync) return _storageFaultCode; }
        }

        public RawbbitSessionState ReadSession()
        {
            lock (_sync) return Clone(_state.Session);
        }

        public RawbbitFaultState ReadFault()
        {
            lock (_sync) return Clone(_state.Fault);
        }

        public RawbbitQueueDiagnostics ReadDiagnostics()
        {
            lock (_sync)
            {
                var result = new RawbbitQueueDiagnostics
                {
                    QueueBytes = _state.Records.Sum(record => record.QueueBytes),
                    QueueGeneration = _state.QueueGeneration,
                    PendingCount = _state.Records.Count(record => !record.Quarantined),
                    QuarantinedCount = _state.Records.Count(record => record.Quarantined),
                    ExpiredCount = _state.TotalExpiredCount,
                    EvictedCount = _state.TotalEvictedCount,
                    QuarantinedExpiredCount = _state.TotalQuarantinedExpiredCount,
                    QuarantinedEvictedCount = _state.TotalQuarantinedEvictedCount,
                    ExplicitlyDiscardedCount = _state.TotalExplicitlyDiscardedCount,
                    FaultCode = _storageFaulted ? _storageFaultCode : (_state.Fault == null ? null : _state.Fault.FaultCode),
                    FaultRevision = _state.Fault == null ? 0 : _state.Fault.Revision,
                    RecoveryProbeRequired = _state.Fault != null && _state.Fault.RequiresProbe,
                    RecoveryProbeValidated = _state.Fault != null && _state.Fault.ProbeValidated
                };
                return result;
            }
        }

        public int PruneExpired(DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                long cutoff = now.UtcDateTime.Ticks - TimeSpan.FromDays(_retentionDays).Ticks;
                List<RawbbitQueueRecord> expiredRecords = _state.Records.Where(record => record.EnqueuedUtcTicks <= cutoff).ToList();
                List<string> expired = expiredRecords.Select(record => record.Handle).ToList();
                if (expired.Count == 0) return 0;
                Commit(new RawbbitStoreTransaction
                {
                    RemoveHandles = expired,
                    ExpiredCount = expired.Count,
                    QuarantinedExpiredCount = expiredRecords.Count(record => record.Quarantined),
                    IncrementQueueGeneration = true
                });
                return expired.Count;
            }
        }

        public RawbbitEnqueueOutcome Enqueue(IList<RawbbitQueueRecord> newRecords, RawbbitSessionState session, DateTimeOffset now)
        {
            if (newRecords == null || newRecords.Count == 0) throw new ArgumentException("At least one event is required.");
            lock (_sync)
            {
                EnsureHealthy();
                PruneExpired(now);
                long newBytes = 0;
                foreach (RawbbitQueueRecord record in newRecords)
                {
                    record.QueueBytes = RawbbitPayload.MeasureRecordBytes(record);
                    newBytes += record.QueueBytes;
                }
                if (newBytes > _maxQueueBytes)
                    return new RawbbitEnqueueOutcome { Stored = false, FailureCode = "event_pair_exceeds_queue_limit" };

                var records = new List<RawbbitQueueRecord>(_state.Records);
                var removed = new List<string>();
                long totalBytes = records.Sum(record => record.QueueBytes) + newBytes;
                foreach (RawbbitQueueRecord oldRecord in records.OrderBy(record => record.Sequence).ThenBy(record => record.OrderInTransaction).ToList())
                {
                    if (totalBytes <= _maxQueueBytes) break;
                    if (newRecords.Any(item => item.Handle == oldRecord.Handle)) continue;
                    removed.Add(oldRecord.Handle);
                    totalBytes -= oldRecord.QueueBytes;
                }
                var removedHandles = new HashSet<string>(removed, StringComparer.Ordinal);
                int removedQuarantinedCount = _state.Records.Count(record => removedHandles.Contains(record.Handle) && record.Quarantined);
                var transaction = new RawbbitStoreTransaction
                {
                    RemoveHandles = removed,
                    Upsert = newRecords.Select(Clone).ToList(),
                    SetSession = true,
                    Session = Clone(session),
                    IncrementQueueGeneration = true,
                    EvictedCount = removed.Count,
                    QuarantinedEvictedCount = removedQuarantinedCount
                };
                for (int i = 0; i < transaction.Upsert.Count; i++)
                {
                    transaction.Upsert[i].Sequence = _state.Sequence + 1;
                    transaction.Upsert[i].OrderInTransaction = i;
                }
                Commit(transaction);
                return new RawbbitEnqueueOutcome { Stored = true, EvictedCount = removed.Count, QueueBytes = totalBytes };
            }
        }

        public IList<RawbbitQueueRecord> GetPendingRecords(int maximumEvents, long maximumBytes, DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.WithdrawalBarrier || _state.Fault != null) return new List<RawbbitQueueRecord>();
                long cutoff = now.UtcDateTime.Ticks - TimeSpan.FromDays(_retentionDays).Ticks;
                var selected = new List<RawbbitQueueRecord>();
                long bodyBytes = 13; // {"events":[ ... ]}
                foreach (RawbbitQueueRecord record in _state.Records.OrderBy(item => item.Sequence).ThenBy(item => item.OrderInTransaction))
                {
                    if (record.Quarantined || record.EnqueuedUtcTicks <= cutoff) continue;
                    long addedBytes = record.PayloadJson == null ? 0 : System.Text.Encoding.UTF8.GetByteCount(record.PayloadJson);
                    if (selected.Count >= maximumEvents || bodyBytes + addedBytes + (selected.Count == 0 ? 0 : 1) > maximumBytes)
                    {
                        break;
                    }
                    selected.Add(Clone(record));
                    bodyBytes += addedBytes + (selected.Count == 1 ? 0 : 1);
                }
                return selected;
            }
        }

        public RawbbitQueueRecord GetOldestPendingRecord(DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.WithdrawalBarrier || _state.Fault != null) return null;
                long cutoff = now.UtcDateTime.Ticks - TimeSpan.FromDays(_retentionDays).Ticks;
                RawbbitQueueRecord record = _state.Records.Where(item => !item.Quarantined && item.EnqueuedUtcTicks > cutoff)
                    .OrderBy(item => item.Sequence).ThenBy(item => item.OrderInTransaction).FirstOrDefault();
                return Clone(record);
            }
        }

        public bool TryDispatch(RawbbitPreparedBatch batch, Func<bool> startRequest)
        {
            if (!Monitor.TryEnter(_sync)) return false;
            try
            {
                if (_storageFaulted || _state.WithdrawalBarrier || _state.PurgePending ||
                    batch == null || batch.RequiresLocalQuarantine || startRequest == null ||
                    batch.Records == null || batch.Records.Count == 0 || batch.Handles == null ||
                    batch.Handles.Count != batch.Records.Count) return false;
                if (batch.IsRecoveryProbe)
                {
                    if (_state.Fault == null || !_state.Fault.RequiresProbe || _state.Fault.ProbeValidated ||
                        _state.Fault.Revision != batch.RecoveryFaultRevision ||
                        _state.QueueGeneration != batch.RecoveryQueueGeneration || batch.Records.Count != 1) return false;
                }
                else if (_state.Fault != null) return false;

                long cutoff = _clock.UtcNow.UtcDateTime.Ticks - TimeSpan.FromDays(_retentionDays).Ticks;
                for (int i = 0; i < batch.Records.Count; i++)
                {
                    RawbbitQueueRecord prepared = batch.Records[i];
                    if (prepared == null || batch.Handles[i] != prepared.Handle) return false;
                    RawbbitQueueRecord current = _state.Records.FirstOrDefault(record => record.Handle == prepared.Handle);
                    if (current == null || current.Quarantined || current.EnqueuedUtcTicks <= cutoff ||
                        current.EnqueuedUtcTicks != prepared.EnqueuedUtcTicks || current.PayloadJson != prepared.PayloadJson ||
                        current.EventId != prepared.EventId) return false;
                }
                // No disk writes here: expiry is checked in memory. Hold the
                // store lock through transport start so a mutation cannot slip
                // between validation and dispatch.
                return startRequest();
            }
            finally { Monitor.Exit(_sync); }
        }

        public RawbbitQueueRecord GetRecoveryProbeRecord(DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.WithdrawalBarrier || _state.PurgePending || _state.Fault == null ||
                    !_state.Fault.RequiresProbe || _state.Fault.ProbeValidated) return null;
                long cutoff = now.UtcDateTime.Ticks - TimeSpan.FromDays(_retentionDays).Ticks;
                RawbbitQueueRecord record = _state.Records.Where(item => !item.Quarantined && item.EnqueuedUtcTicks > cutoff)
                    .OrderBy(item => item.Sequence).ThenBy(item => item.OrderInTransaction).FirstOrDefault();
                return Clone(record);
            }
        }

        public void MarkRecoveryProbeValidated(long expectedRevision, long expectedQueueGeneration, string handle, DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.Fault == null || _state.Fault.Revision != expectedRevision || !_state.Fault.RequiresProbe)
                    throw new InvalidOperationException("The recovery probe fault revision is stale.");
                if (_state.QueueGeneration != expectedQueueGeneration)
                    throw new InvalidOperationException("The recovery probe queue generation is stale.");
                RawbbitQueueRecord record = _state.Records.FirstOrDefault(item => item.Handle == handle && !item.Quarantined);
                if (record == null || !RawbbitPayload.ValidateFrozenEvent(record.PayloadJson, _appId, _environment, _platform, _maxRequestBytes))
                    throw new InvalidOperationException("The recovery probe record is unavailable or invalid.");

                RawbbitFaultState fault = Clone(_state.Fault);
                fault.ProbeValidated = true;
                Commit(new RawbbitStoreTransaction
                {
                    RemoveHandles = new List<string> { handle },
                    SetFault = true,
                    Fault = fault,
                    AddReceipts = new List<RawbbitRecoveryReceipt>
                    {
                        NewReceipt("recovery_probe", "server_accepted_one_event", "sdk_v1", 1, expectedRevision, now)
                    },
                    IncrementQueueGeneration = true
                });
            }
        }

        public void Acknowledge(IList<string> handles)
        {
            lock (_sync)
            {
                EnsureHealthy();
                var existing = new HashSet<string>(_state.Records.Select(record => record.Handle), StringComparer.Ordinal);
                var quarantinedSet = new HashSet<string>(_state.Records.Where(record => record.Quarantined).Select(record => record.Handle), StringComparer.Ordinal);
                List<string> toRemove = handles.Where(handle => existing.Contains(handle) && !quarantinedSet.Contains(handle)).Distinct(StringComparer.Ordinal).ToList();
                if (toRemove.Count == 0) return;
                Commit(new RawbbitStoreTransaction { RemoveHandles = toRemove, IncrementQueueGeneration = true });
            }
        }

        public void Quarantine(IList<string> handles, int statusCode, string faultCode, DateTimeOffset now, bool requiresProbe)
        {
            if (statusCode != 0 && (statusCode < 400 || statusCode > 599))
                throw new ArgumentOutOfRangeException("statusCode");
            lock (_sync)
            {
                EnsureHealthy();
                var wanted = new HashSet<string>(handles, StringComparer.Ordinal);
                List<RawbbitQueueRecord> changed = _state.Records.Where(record => wanted.Contains(record.Handle)).Select(Clone).ToList();
                foreach (RawbbitQueueRecord record in changed)
                {
                    record.Quarantined = true;
                    record.HttpStatusCode = statusCode;
                    record.FaultCode = SafeCode(faultCode);
                }
                var fault = new RawbbitFaultState
                {
                    FaultCode = SafeCode(faultCode),
                    HttpStatusCode = statusCode,
                    Revision = _state.Sequence + 1,
                    CreatedUtcTicks = now.UtcDateTime.Ticks,
                    RequiresProbe = requiresProbe,
                    ProbeValidated = false
                };
                Commit(new RawbbitStoreTransaction
                {
                    Upsert = changed,
                    SetFault = true,
                    Fault = fault,
                    IncrementQueueGeneration = true
                });
            }
        }

        public void LatchFault(string faultCode, int statusCode, DateTimeOffset now, bool requiresProbe)
        {
            lock (_sync)
            {
                EnsureHealthy();
                var fault = new RawbbitFaultState
                {
                    FaultCode = SafeCode(faultCode),
                    HttpStatusCode = statusCode,
                    Revision = _state.Sequence + 1,
                    CreatedUtcTicks = now.UtcDateTime.Ticks,
                    RequiresProbe = requiresProbe,
                    ProbeValidated = false
                };
                Commit(new RawbbitStoreTransaction { SetFault = true, Fault = fault });
            }
        }

        public IList<RawbbitQuarantinedRecordInfo> ListQuarantined()
        {
            lock (_sync)
            {
                return _state.Records.Where(record => record.Quarantined).OrderBy(record => record.Sequence).ThenBy(record => record.OrderInTransaction).Select(record => new RawbbitQuarantinedRecordInfo
                {
                    Handle = record.Handle,
                    HttpStatusCode = record.HttpStatusCode,
                    FaultCode = record.FaultCode,
                    EnqueuedAtUtc = new DateTimeOffset(record.EnqueuedUtcTicks, TimeSpan.Zero)
                }).ToList();
            }
        }

        public RawbbitRecoveryResult DiscardQuarantined(IList<string> handles, long expectedRevision, long expectedQueueGeneration, string reasonCode, string policyVersion, DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.Fault == null || _state.Fault.Revision != expectedRevision)
                    return RecoveryFailure("stale_fault_revision");
                if (_state.QueueGeneration != expectedQueueGeneration) return RecoveryFailure("stale_queue_generation");
                if (handles == null || handles.Count == 0 || handles.Count > 256 || !ValidReasonCode("dispose", reasonCode) || !ValidRecoveryCode(policyVersion))
                    return RecoveryFailure("invalid_recovery_request");
                var selected = new HashSet<string>(handles, StringComparer.Ordinal);
                if (selected.Count != handles.Count) return RecoveryFailure("duplicate_record_handle");
                var found = _state.Records.Where(record => selected.Contains(record.Handle) && record.Quarantined).ToList();
                if (found.Count != selected.Count) return RecoveryFailure("record_not_quarantined_or_not_found");
                var receipt = NewReceipt("dispose_quarantined", reasonCode, policyVersion, found.Count, expectedRevision, now);
                Commit(new RawbbitStoreTransaction
                {
                    RemoveHandles = selected.ToList(),
                    AddReceipts = new List<RawbbitRecoveryReceipt> { receipt },
                    IncrementQueueGeneration = true,
                    ExplicitlyDiscardedCount = found.Count
                });
                return RecoverySuccess("quarantined_records_disposed");
            }
        }

        public RawbbitRecoveryResult RetryQuarantined(IList<string> handles, long expectedRevision, long expectedQueueGeneration, string reasonCode, string policyVersion, DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.Fault == null || _state.Fault.Revision != expectedRevision) return RecoveryFailure("stale_fault_revision");
                if (_state.QueueGeneration != expectedQueueGeneration) return RecoveryFailure("stale_queue_generation");
                if (handles == null || handles.Count == 0 || handles.Count > 256 || !ValidReasonCode("retry", reasonCode) || !ValidRecoveryCode(policyVersion))
                    return RecoveryFailure("invalid_recovery_request");
                var selected = new HashSet<string>(handles, StringComparer.Ordinal);
                if (selected.Count != handles.Count) return RecoveryFailure("duplicate_record_handle");
                List<RawbbitQueueRecord> changed = _state.Records.Where(record => selected.Contains(record.Handle) && record.Quarantined).Select(Clone).ToList();
                if (changed.Count != selected.Count) return RecoveryFailure("record_not_quarantined_or_not_found");
                foreach (RawbbitQueueRecord record in changed)
                {
                    if (!RawbbitPayload.ValidateFrozenEvent(record.PayloadJson, _appId, _environment, _platform, _maxRequestBytes))
                        return RecoveryFailure("frozen_event_validation_failed");
                    record.Quarantined = false;
                    record.HttpStatusCode = 0;
                    record.FaultCode = null;
                }
                Commit(new RawbbitStoreTransaction
                {
                    Upsert = changed,
                    AddReceipts = new List<RawbbitRecoveryReceipt> { NewReceipt("retry_quarantined", reasonCode, policyVersion, changed.Count, expectedRevision, now) },
                    IncrementQueueGeneration = true
                });
                return RecoverySuccess("quarantined_records_released_for_validation");
            }
        }

        public RawbbitRecoveryResult ValidateAndResume(long expectedRevision, long expectedQueueGeneration, string reasonCode, string policyVersion, DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.Fault == null || _state.Fault.Revision != expectedRevision) return RecoveryFailure("stale_fault_revision");
                if (_state.QueueGeneration != expectedQueueGeneration) return RecoveryFailure("stale_queue_generation");
                if (!ValidReasonCode("validate", reasonCode) || !ValidRecoveryCode(policyVersion)) return RecoveryFailure("invalid_recovery_request");
                if (_state.Records.Any(record => record.Quarantined)) return RecoveryFailure("quarantined_records_remain");
                var invalidRecords = new List<RawbbitQueueRecord>();
                foreach (RawbbitQueueRecord record in _state.Records)
                {
                    if (!RawbbitPayload.ValidateFrozenEvent(record.PayloadJson, _appId, _environment, _platform, _maxRequestBytes))
                    {
                        RawbbitQueueRecord quarantined = Clone(record);
                        quarantined.Quarantined = true;
                        quarantined.HttpStatusCode = 0;
                        quarantined.FaultCode = "recovery_validation_invalid_event";
                        invalidRecords.Add(quarantined);
                        continue;
                    }
                    if (record.QueueBytes != RawbbitPayload.MeasureRecordBytes(record)) return RecoveryFailure("stored_record_size_mismatch");
                }
                if (invalidRecords.Count > 0)
                {
                    foreach (List<RawbbitQueueRecord> chunk in GroupRecordsForTransactions(invalidRecords))
                    {
                        var fault = new RawbbitFaultState
                        {
                            FaultCode = "recovery_validation_requires_review",
                            HttpStatusCode = 0,
                            Revision = _state.Sequence + 1,
                            CreatedUtcTicks = now.UtcDateTime.Ticks,
                            RequiresProbe = true,
                            ProbeValidated = false
                        };
                        Commit(new RawbbitStoreTransaction
                        {
                            Upsert = chunk,
                            SetFault = true,
                            Fault = fault,
                            AddReceipts = new List<RawbbitRecoveryReceipt>
                            {
                                NewReceipt("validation_quarantine", reasonCode, policyVersion, chunk.Count, expectedRevision, now)
                            },
                            IncrementQueueGeneration = true
                        });
                    }
                    return new RawbbitRecoveryResult
                    {
                        Succeeded = false,
                        SafeDiagnosticCode = "invalid_records_quarantined_for_review",
                        FaultRevision = _state.Fault.Revision,
                        QueueGeneration = _state.QueueGeneration
                    };
                }
                if (_state.Fault.RequiresProbe && !_state.Fault.ProbeValidated) return RecoveryFailure("recovery_probe_required");
                Commit(new RawbbitStoreTransaction
                {
                    SetFault = true,
                    Fault = null,
                    AddReceipts = new List<RawbbitRecoveryReceipt> { NewReceipt("validate_and_resume", reasonCode, policyVersion, 0, expectedRevision, now) },
                    IncrementQueueGeneration = true
                });
                return RecoverySuccess("delivery_fault_cleared");
            }
        }

        private IList<List<RawbbitQueueRecord>> GroupRecordsForTransactions(IList<RawbbitQueueRecord> records)
        {
            const int transactionOverheadBudget = 4096;
            long maximumEntriesBytes = MaximumJournalFrameBytes - (64 * 1024);
            var groups = new List<List<RawbbitQueueRecord>>();
            var current = new List<RawbbitQueueRecord>();
            long currentBytes = transactionOverheadBudget;

            foreach (RawbbitQueueRecord record in records)
            {
                long encodedBytes = Encoding.UTF8.GetByteCount(JsonConvert.SerializeObject(record, _jsonSettings));
                if (encodedBytes + transactionOverheadBudget > maximumEntriesBytes)
                    throw new InvalidOperationException("A single invalid record exceeds the bounded recovery transaction size.");
                if (current.Count > 0 && currentBytes + encodedBytes + 1 > maximumEntriesBytes)
                {
                    groups.Add(current);
                    current = new List<RawbbitQueueRecord>();
                    currentBytes = transactionOverheadBudget;
                }
                current.Add(record);
                currentBytes += encodedBytes + 1;
            }
            if (current.Count > 0) groups.Add(current);
            return groups;
        }

        public int WithdrawAndPurge(DateTimeOffset now)
        {
            lock (_sync)
            {
                EnsureHealthy();
                int removed = _state.Records.Count;
                var receipt = NewReceipt("consent_withdrawal_purge", "consent_withdrawn", "sdk_v1", removed, _state.Fault == null ? 0 : _state.Fault.Revision, now);
                // Persist the revocation barrier first. If the process stops
                // before record deletion commits, startup completes the purge
                // before it can accept fresh consent or dispatch anything.
                Commit(new RawbbitStoreTransaction
                {
                    SetSession = true,
                    Session = new RawbbitSessionState(),
                    SetWithdrawalBarrier = true,
                    WithdrawalBarrier = true,
                    SetPurgePending = true,
                    PurgePending = true,
                    IncrementQueueGeneration = true
                });
                Commit(new RawbbitStoreTransaction
                {
                    ClearRecords = true,
                    SetSession = true,
                    Session = new RawbbitSessionState(),
                    SetPurgePending = true,
                    PurgePending = true,
                    IncrementQueueGeneration = true,
                    AddReceipts = new List<RawbbitRecoveryReceipt> { receipt }
                });
                SanitizePersistedEventFiles();
                Commit(new RawbbitStoreTransaction { SetPurgePending = true, PurgePending = false });
                return removed;
            }
        }

        public void GrantConsent()
        {
            lock (_sync)
            {
                EnsureHealthy();
                if (_state.PurgePending) throw new InvalidOperationException("A consent withdrawal purge must finish before consent can be granted.");
                if (!_state.WithdrawalBarrier) return;
                Commit(new RawbbitStoreTransaction { SetWithdrawalBarrier = true, WithdrawalBarrier = false });
            }
        }

        internal void SetSession(RawbbitSessionState session)
        {
            lock (_sync)
            {
                EnsureHealthy();
                Commit(new RawbbitStoreTransaction { SetSession = true, Session = Clone(session) });
            }
        }

        private void EnsureInstallationId()
        {
            if (!String.IsNullOrEmpty(_state.InstallationId)) return;
            Commit(new RawbbitStoreTransaction { SetInstallationId = true, InstallationId = Guid.NewGuid().ToString("D") });
        }

        private void LoadOrInitialize()
        {
            lock (_sync)
            {
                List<string> snapshotPaths = Directory.GetFiles(_root, "snapshot-*.json").OrderByDescending(path => SnapshotSequence(path)).ToList();
                RawbbitPersistentState loaded = null;
                foreach (string path in snapshotPaths)
                {
                    try
                    {
                        RawbbitSnapshotEnvelope envelope = JsonConvert.DeserializeObject<RawbbitSnapshotEnvelope>(File.ReadAllText(path), _jsonSettings);
                        if (envelope != null && envelope.FormatVersion != 1)
                            throw new InvalidDataException("The Rawbbit queue checkpoint format is unsupported.");
                        if (envelope == null || envelope.StateJson == null || Hash(Encoding.UTF8.GetBytes(envelope.StateJson)) != envelope.Sha256) continue;
                        JObject rawState = JObject.Parse(envelope.StateJson);
                        if (rawState["FormatVersion"] == null || rawState["FormatVersion"].Type != JTokenType.Integer || rawState["FormatVersion"].Value<int>() != 1)
                            throw new InvalidDataException("The Rawbbit queue state format is unsupported.");
                        if (rawState["Sequence"] == null || rawState["Sequence"].Type != JTokenType.Integer ||
                            rawState["Session"] == null || rawState["Session"].Type != JTokenType.Object ||
                            rawState["Records"] == null || rawState["Records"].Type != JTokenType.Array ||
                            rawState["Receipts"] == null || rawState["Receipts"].Type != JTokenType.Array ||
                            rawState["QueueGeneration"] == null || rawState["TotalExpiredCount"] == null ||
                            rawState["TotalEvictedCount"] == null || rawState["TotalQuarantinedExpiredCount"] == null ||
                            rawState["TotalQuarantinedEvictedCount"] == null || rawState["TotalExplicitlyDiscardedCount"] == null ||
                            rawState["WithdrawalBarrier"] == null || rawState["PurgePending"] == null) continue;
                        RawbbitPersistentState candidate = JsonConvert.DeserializeObject<RawbbitPersistentState>(envelope.StateJson, _jsonSettings);
                        if (!ValidState(candidate)) continue;
                        loaded = candidate;
                        break;
                    }
                    catch (InvalidDataException) { throw; }
                    catch { }
                }

                bool hasJournal = Directory.GetFiles(_root, "journal-*.bin").Any();
                if (snapshotPaths.Count == 0 && !hasJournal)
                {
                    string[] entries = Directory.GetFileSystemEntries(_root);
                    if (entries.Any(path => !IsInitialCheckpointTemp(path)))
                        throw new InvalidDataException("Unrecognized Rawbbit store contents require explicit recovery; no new store was initialized.");
                    // Sequence-zero checkpoint creation precedes installation
                    // identity and every event commit. Only its own temporary
                    // files may be abandoned safely after an interrupted open.
                    foreach (string path in entries) File.Delete(path);
                }
                if (loaded == null && snapshotPaths.Count > 0 && !hasJournal)
                    throw new InvalidDataException("No valid Rawbbit queue checkpoint is recoverable.");
                if (loaded == null && hasJournal && !Directory.GetFiles(_root, "journal-*.bin").Any(path => JournalBaseSequence(path) == 0))
                    throw new InvalidDataException("No base checkpoint exists for the Rawbbit queue journal.");
                _state = loaded ?? new RawbbitPersistentState();
                List<JournalFrame> frames = ReadAllJournalFrames();
                foreach (JournalFrame frame in frames.Where(frame => frame.Transaction.Sequence > _state.Sequence).OrderBy(frame => frame.Transaction.Sequence))
                {
                    if (frame.Transaction.Sequence != _state.Sequence + 1)
                        throw new InvalidDataException("The Rawbbit queue journal has a sequence gap.");
                    Apply(frame.Transaction);
                }

                if (_state.Records.Sum(record => record.QueueBytes) > _maxQueueBytes ||
                    _state.Records.Any(record => String.IsNullOrWhiteSpace(record.Handle) || String.IsNullOrWhiteSpace(record.PayloadJson) || record.QueueBytes != RawbbitPayload.MeasureRecordBytes(record)))
                    throw new InvalidDataException("The Rawbbit queue record accounting is invalid.");

                if (_state.Sequence == 0 && snapshotPaths.Count == 0 && !Directory.GetFiles(_root, "journal-*.bin").Any())
                {
                    WriteSnapshot(_state);
                    _activeJournal = JournalPath(0);
                    CreateJournalIfMissing(_activeJournal);
                }
                else
                {
                    _activeJournal = Directory.GetFiles(_root, "journal-*.bin")
                        .OrderByDescending(JournalBaseSequence)
                        .FirstOrDefault();
                    if (_activeJournal == null)
                    {
                        _activeJournal = JournalPath(_state.Sequence);
                        CreateJournalIfMissing(_activeJournal);
                    }
                    CleanupOldGenerations();
                }
                if (_state.PurgePending) CompleteInterruptedPurge();
            }
        }

        private void CompleteInterruptedPurge()
        {
            int removed = _state.Records.Count;
            var receipt = NewReceipt("consent_withdrawal_purge_recovery", "consent_withdrawn", "sdk_v1", removed,
                _state.Fault == null ? 0 : _state.Fault.Revision, _clock.UtcNow);
            Commit(new RawbbitStoreTransaction
            {
                ClearRecords = true,
                SetSession = true,
                Session = new RawbbitSessionState(),
                SetPurgePending = true,
                PurgePending = true,
                IncrementQueueGeneration = true,
                AddReceipts = new List<RawbbitRecoveryReceipt> { receipt }
            });
            SanitizePersistedEventFiles();
            Commit(new RawbbitStoreTransaction { SetPurgePending = true, PurgePending = false });
        }

        private static bool IsInitialCheckpointTemp(string path)
        {
            const string prefix = "snapshot-00000000000000000000.json.";
            const string suffix = ".tmp";
            string name = Path.GetFileName(path);
            if (!File.Exists(path) || name.Length != prefix.Length + 32 + suffix.Length || !name.StartsWith(prefix, StringComparison.Ordinal) ||
                !name.EndsWith(suffix, StringComparison.Ordinal)) return false;
            string identifier = name.Substring(prefix.Length, name.Length - prefix.Length - suffix.Length);
            Guid parsed;
            return Guid.TryParseExact(identifier, "N", out parsed);
        }

        private void SanitizePersistedEventFiles()
        {
            EnsureHealthy();
            // First create a durable, payload-free checkpoint. Only after that
            // checkpoint exists may old journals/checkpoints be removed.
            WriteSnapshot(_state);
            string sanitizedSnapshot = SnapshotPath(_state.Sequence);
            foreach (string path in Directory.GetFiles(_root, "snapshot-*.json"))
                if (!String.Equals(path, sanitizedSnapshot, StringComparison.Ordinal)) File.Delete(path);
            foreach (string path in Directory.GetFiles(_root, "journal-*.bin")) File.Delete(path);
            foreach (string path in Directory.GetFiles(_root, "*.tmp")) File.Delete(path);

            _activeJournal = JournalPath(_state.Sequence);
            CreateJournalIfMissing(_activeJournal);
        }

        private List<JournalFrame> ReadAllJournalFrames()
        {
            var frames = new List<JournalFrame>();
            string[] paths = Directory.GetFiles(_root, "journal-*.bin").OrderBy(JournalBaseSequence).ToArray();
            foreach (string path in paths)
            {
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.Read))
                {
                    long lastGoodOffset = 0;
                    bool truncated = false;
                    while (stream.Position < stream.Length)
                    {
                        long frameStart = stream.Position;
                        if (stream.Length - frameStart < 4)
                        {
                            stream.SetLength(lastGoodOffset);
                            truncated = true;
                            break;
                        }
                        int length = ReadInt32(stream);
                        if (length < 1 || length > MaximumJournalFrameBytes)
                            throw new InvalidDataException("The Rawbbit queue journal frame length is invalid.");
                        if (stream.Length - stream.Position < 32L + length)
                        {
                            stream.SetLength(lastGoodOffset);
                            truncated = true;
                            break;
                        }
                        byte[] expectedHash = ReadBytes(stream, 32);
                        byte[] payload = ReadBytes(stream, length);
                        if (!FixedTimeEquals(expectedHash, Sha256(payload)))
                            throw new InvalidDataException("The Rawbbit queue journal checksum is invalid.");
                        string transactionJson = Encoding.UTF8.GetString(payload);
                        JObject rawTransaction = JObject.Parse(transactionJson);
                        if (rawTransaction["FormatVersion"] == null || rawTransaction["FormatVersion"].Type != JTokenType.Integer || rawTransaction["FormatVersion"].Value<int>() != 1)
                            throw new InvalidDataException("The Rawbbit queue transaction format is unsupported.");
                        RawbbitStoreTransaction transaction = JsonConvert.DeserializeObject<RawbbitStoreTransaction>(transactionJson, _jsonSettings);
                        if (transaction == null || transaction.FormatVersion != 1 || transaction.Sequence < 1)
                            throw new InvalidDataException("The Rawbbit queue journal transaction is invalid.");
                        frames.Add(new JournalFrame { Transaction = transaction, Path = path, StartOffset = frameStart });
                        lastGoodOffset = stream.Position;
                    }
                    if (truncated) stream.Flush(true);
                }
            }
            return frames;
        }

        private void Commit(RawbbitStoreTransaction transaction)
        {
            EnsureHealthy();
            transaction.Sequence = _state.Sequence + 1;
            if (transaction.RemoveHandles == null) transaction.RemoveHandles = new List<string>();
            if (transaction.Upsert == null) transaction.Upsert = new List<RawbbitQueueRecord>();
            if (transaction.AddReceipts == null) transaction.AddReceipts = new List<RawbbitRecoveryReceipt>();
            byte[] payload = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(transaction, _jsonSettings));
            if (payload.Length > MaximumJournalFrameBytes) throw new InvalidOperationException("Queue transaction exceeds the bounded journal frame size.");
            try
            {
                if (_activeJournal == null)
                {
                    _activeJournal = JournalPath(_state.Sequence);
                    CreateJournalIfMissing(_activeJournal);
                }
                long maximumJournalBytes = _maxQueueBytes + (2L * MaximumJournalFrameBytes);
                var info = new FileInfo(_activeJournal);
                if (info.Exists && info.Length + payload.Length + 36L > maximumJournalBytes)
                    Compact();
                AppendFrame(_activeJournal, payload);
            }
            catch
            {
                _storageFaulted = true;
                _storageFaultCode = "queue_commit_failed";
                throw;
            }
            Apply(transaction);
            try
            {
                if (new FileInfo(_activeJournal).Length >= _journalCompactionBytes) Compact();
            }
            catch
            {
                // Preserve the committed transaction, but stop accepting more
                // writes until compaction can be recovered; otherwise repeated
                // failures could grow temporary/old generations without bound.
                _storageFaulted = true;
                _storageFaultCode = "queue_compaction_failed";
            }
        }

        private void Apply(RawbbitStoreTransaction transaction)
        {
            if (transaction.ClearRecords) _state.Records.Clear();
            if (transaction.RemoveHandles != null && transaction.RemoveHandles.Count > 0)
            {
                var handles = new HashSet<string>(transaction.RemoveHandles, StringComparer.Ordinal);
                _state.Records.RemoveAll(record => handles.Contains(record.Handle));
            }
            if (transaction.Upsert != null)
            {
                foreach (RawbbitQueueRecord record in transaction.Upsert)
                {
                    _state.Records.RemoveAll(existing => existing.Handle == record.Handle);
                    _state.Records.Add(Clone(record));
                }
            }
            if (transaction.SetSession) _state.Session = Clone(transaction.Session) ?? new RawbbitSessionState();
            if (transaction.SetInstallationId) _state.InstallationId = transaction.InstallationId;
            if (transaction.SetCollectorEndpoint) _state.CollectorEndpoint = transaction.CollectorEndpoint;
            if (transaction.SetFault) _state.Fault = Clone(transaction.Fault);
            if (transaction.SetWithdrawalBarrier) _state.WithdrawalBarrier = transaction.WithdrawalBarrier;
            if (transaction.SetPurgePending) _state.PurgePending = transaction.PurgePending;
            if (transaction.IncrementQueueGeneration) _state.QueueGeneration++;
            _state.TotalExpiredCount += transaction.ExpiredCount;
            _state.TotalEvictedCount += transaction.EvictedCount;
            _state.TotalQuarantinedExpiredCount += transaction.QuarantinedExpiredCount;
            _state.TotalQuarantinedEvictedCount += transaction.QuarantinedEvictedCount;
            _state.TotalExplicitlyDiscardedCount += transaction.ExplicitlyDiscardedCount;
            if (transaction.AddReceipts != null)
            {
                _state.Receipts.AddRange(transaction.AddReceipts.Select(Clone));
                if (_state.Receipts.Count > MaximumReceipts)
                    _state.Receipts.RemoveRange(0, _state.Receipts.Count - MaximumReceipts);
            }
            _state.Sequence = transaction.Sequence;
        }

        private void Compact()
        {
            EnsureHealthy();
            long previousSnapshotSequence = Directory.GetFiles(_root, "snapshot-*.json")
                .Select(SnapshotSequence).Where(sequence => sequence < _state.Sequence).DefaultIfEmpty(0).Max();
            WriteSnapshot(_state);
            string newJournal = JournalPath(_state.Sequence);
            CreateJournalIfMissing(newJournal);
            _activeJournal = newJournal;
            string[] journals = Directory.GetFiles(_root, "journal-*.bin");
            foreach (string journal in journals)
            {
                long baseSequence = JournalBaseSequence(journal);
                if (baseSequence < previousSnapshotSequence)
                    File.Delete(journal);
            }
            CleanupOldGenerations();
        }

        private void WriteSnapshot(RawbbitPersistentState state)
        {
            string stateJson = JsonConvert.SerializeObject(state, _jsonSettings);
            var envelope = new RawbbitSnapshotEnvelope
            {
                FormatVersion = 1,
                StateJson = stateJson,
                Sha256 = Hash(Encoding.UTF8.GetBytes(stateJson))
            };
            string finalPath = SnapshotPath(state.Sequence);
            if (File.Exists(finalPath))
            {
                try
                {
                    RawbbitSnapshotEnvelope existing = JsonConvert.DeserializeObject<RawbbitSnapshotEnvelope>(File.ReadAllText(finalPath), _jsonSettings);
                    if (existing != null && existing.FormatVersion == 1 && existing.Sha256 == Hash(Encoding.UTF8.GetBytes(existing.StateJson))) return;
                }
                catch { }
                File.Delete(finalPath);
            }
            string tempPath = finalPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            byte[] bytes = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(envelope, _jsonSettings));
            try
            {
                using (var stream = new FileStream(tempPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true);
                }
                File.Move(tempPath, finalPath);
            }
            finally
            {
                if (File.Exists(tempPath)) File.Delete(tempPath);
            }
        }

        private void CleanupOldGenerations()
        {
            List<string> snapshots = Directory.GetFiles(_root, "snapshot-*.json").OrderByDescending(SnapshotSequence).ToList();
            if (snapshots.Count < 2) return;
            long fallbackSequence = SnapshotSequence(snapshots[1]);
            foreach (string oldSnapshot in snapshots.Skip(2))
                File.Delete(oldSnapshot);
            foreach (string oldJournal in Directory.GetFiles(_root, "journal-*.bin"))
            {
                if (JournalBaseSequence(oldJournal) < fallbackSequence)
                {
                    File.Delete(oldJournal);
                }
            }
            foreach (string temp in Directory.GetFiles(_root, "*.tmp"))
                File.Delete(temp);
        }

        private void CreateJournalIfMissing(string path)
        {
            if (File.Exists(path)) return;
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read)) stream.Flush(true);
        }

        private static void AppendFrame(string path, byte[] payload)
        {
            byte[] digest = Sha256(payload);
            using (var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read))
            {
                WriteInt32(stream, payload.Length);
                stream.Write(digest, 0, digest.Length);
                stream.Write(payload, 0, payload.Length);
                stream.Flush(true);
            }
        }

        private void EnsureHealthy()
        {
            if (_storageFaulted) throw new IOException(_storageFaultCode ?? "queue_storage_faulted");
        }

        private static bool ValidState(RawbbitPersistentState state)
        {
            if (state == null || state.FormatVersion != 1 || state.Sequence < 0 || state.QueueGeneration < 0 ||
                state.TotalExpiredCount < 0 || state.TotalEvictedCount < 0 || state.TotalQuarantinedExpiredCount < 0 ||
                state.TotalQuarantinedEvictedCount < 0 || state.TotalExplicitlyDiscardedCount < 0 ||
                state.TotalQuarantinedExpiredCount > state.TotalExpiredCount || state.TotalQuarantinedEvictedCount > state.TotalEvictedCount ||
                state.Records == null || state.Session == null || state.Receipts == null)
                return false;
            if (state.InstallationId != null)
            {
                Guid installationId;
                if (!Guid.TryParse(state.InstallationId, out installationId)) return false;
            }
            if (state.Session.SessionId == null)
            {
                if (state.Session.LastActivityUtcTicks != 0 || state.Session.EventCounter != 0) return false;
            }
            else if (String.IsNullOrWhiteSpace(state.Session.SessionId) || state.Session.SessionId.Length > 128 ||
                state.Session.LastActivityUtcTicks <= 0 || state.Session.LastActivityUtcTicks > DateTime.MaxValue.Ticks || state.Session.EventCounter < 1)
            {
                return false;
            }
            if (state.Session.SessionId != null)
                for (int i = 0; i < state.Session.SessionId.Length; i++) if (Char.IsControl(state.Session.SessionId[i])) return false;
            if (state.Receipts.Count > MaximumReceipts) return false;
            var handles = new HashSet<string>(StringComparer.Ordinal);
            foreach (RawbbitQueueRecord record in state.Records)
            {
                Guid eventId;
                if (record == null || String.IsNullOrWhiteSpace(record.Handle) || record.Handle.Length > 64 || !handles.Add(record.Handle) ||
                    String.IsNullOrWhiteSpace(record.EventId) || !Guid.TryParse(record.EventId, out eventId) ||
                    String.IsNullOrWhiteSpace(record.PayloadJson) || record.Sequence < 1 || record.OrderInTransaction < 0 || record.QueueBytes < 0 ||
                    record.EnqueuedUtcTicks <= 0 || record.EnqueuedUtcTicks > DateTime.MaxValue.Ticks ||
                    (record.Quarantined && ((record.HttpStatusCode != 0 && (record.HttpStatusCode < 400 || record.HttpStatusCode > 599)) ||
                        String.IsNullOrWhiteSpace(record.FaultCode))))
                    return false;
            }
            if (state.PurgePending && !state.WithdrawalBarrier) return false;
            if (state.Fault != null && (String.IsNullOrWhiteSpace(state.Fault.FaultCode) || !ValidRecoveryCode(state.Fault.FaultCode) ||
                state.Fault.Revision < 1 || state.Fault.Revision > state.Sequence || (state.Fault.ProbeValidated && !state.Fault.RequiresProbe))) return false;
            foreach (RawbbitRecoveryReceipt receipt in state.Receipts)
            {
                Guid operationId;
                if (receipt == null || String.IsNullOrWhiteSpace(receipt.OperationId) || !Guid.TryParseExact(receipt.OperationId, "N", out operationId) ||
                    !ValidRecoveryCode(receipt.Operation) || !ValidRecoveryCode(receipt.ReasonCode) || !ValidRecoveryCode(receipt.PolicyVersion) ||
                    receipt.AffectedCount < 0 || receipt.FaultRevision < 0 || receipt.CreatedUtcTicks <= 0 || receipt.CreatedUtcTicks > DateTime.MaxValue.Ticks)
                    return false;
            }
            return true;
        }

        private static RawbbitRecoveryReceipt NewReceipt(string operation, string reason, string policy, int count, long revision, DateTimeOffset now)
        {
            return new RawbbitRecoveryReceipt
            {
                OperationId = Guid.NewGuid().ToString("N"),
                Operation = operation,
                ReasonCode = reason,
                PolicyVersion = policy,
                AffectedCount = count,
                FaultRevision = revision,
                CreatedUtcTicks = now.UtcDateTime.Ticks
            };
        }

        private static bool ValidRecoveryCode(string value)
        {
            if (String.IsNullOrWhiteSpace(value) || value.Length > 64) return false;
            for (int i = 0; i < value.Length; i++)
            {
                char ch = value[i];
                if (!(Char.IsLetterOrDigit(ch) || ch == '_' || ch == '-' || ch == '.')) return false;
            }
            return true;
        }

        private static bool ValidReasonCode(string operation, string value)
        {
            if (!ValidRecoveryCode(value)) return false;
            switch (operation)
            {
                case "dispose":
                    return value == "invalid_frozen_event" || value == "collector_contract_rejected" || value == "unsupported_frozen_payload";
                case "retry":
                    return value == "configuration_corrected" || value == "collector_limit_corrected" || value == "sdk_fix_validated";
                case "validate":
                    return value == "configuration_corrected" || value == "collector_limit_corrected" ||
                        value == "sdk_fix_validated" || value == "post_disposal_validation";
                default:
                    return false;
            }
        }

        private static string SafeCode(string value)
        {
            if (!ValidRecoveryCode(value)) return "transport_fault";
            return value;
        }

        private static RawbbitRecoveryResult RecoveryFailure(string code)
        {
            return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = code };
        }

        private RawbbitRecoveryResult RecoverySuccess(string code)
        {
            return new RawbbitRecoveryResult
            {
                Succeeded = true,
                SafeDiagnosticCode = code,
                FaultRevision = _state.Fault == null ? 0 : _state.Fault.Revision,
                QueueGeneration = _state.QueueGeneration
            };
        }

        private static RawbbitQueueRecord Clone(RawbbitQueueRecord record)
        {
            return record == null ? null : JsonConvert.DeserializeObject<RawbbitQueueRecord>(JsonConvert.SerializeObject(record));
        }

        private static RawbbitSessionState Clone(RawbbitSessionState session)
        {
            return session == null ? null : new RawbbitSessionState
            {
                SessionId = session.SessionId,
                LastActivityUtcTicks = session.LastActivityUtcTicks,
                EventCounter = session.EventCounter
            };
        }

        private static RawbbitFaultState Clone(RawbbitFaultState fault)
        {
            return fault == null ? null : new RawbbitFaultState
            {
                FaultCode = fault.FaultCode,
                HttpStatusCode = fault.HttpStatusCode,
                Revision = fault.Revision,
                CreatedUtcTicks = fault.CreatedUtcTicks,
                RequiresProbe = fault.RequiresProbe,
                ProbeValidated = fault.ProbeValidated
            };
        }

        private static RawbbitRecoveryReceipt Clone(RawbbitRecoveryReceipt receipt)
        {
            return receipt == null ? null : new RawbbitRecoveryReceipt
            {
                OperationId = receipt.OperationId,
                Operation = receipt.Operation,
                ReasonCode = receipt.ReasonCode,
                PolicyVersion = receipt.PolicyVersion,
                AffectedCount = receipt.AffectedCount,
                FaultRevision = receipt.FaultRevision,
                CreatedUtcTicks = receipt.CreatedUtcTicks
            };
        }

        private long SnapshotSequence(string path)
        {
            return ParseSequence(path, "snapshot-", ".json");
        }

        private long JournalBaseSequence(string path)
        {
            return ParseSequence(path, "journal-", ".bin");
        }

        private static long ParseSequence(string path, string prefix, string suffix)
        {
            string name = Path.GetFileName(path);
            if (!name.StartsWith(prefix, StringComparison.Ordinal) || !name.EndsWith(suffix, StringComparison.Ordinal)) return 0;
            long result;
            return Int64.TryParse(name.Substring(prefix.Length, name.Length - prefix.Length - suffix.Length), NumberStyles.None, CultureInfo.InvariantCulture, out result) ? result : 0;
        }

        private string SnapshotPath(long sequence) { return Path.Combine(_root, "snapshot-" + sequence.ToString("D20", CultureInfo.InvariantCulture) + ".json"); }
        private string JournalPath(long baseSequence) { return Path.Combine(_root, "journal-" + baseSequence.ToString("D20", CultureInfo.InvariantCulture) + ".bin"); }

        private static string Hash(byte[] bytes)
        {
            byte[] hash = Sha256(bytes);
            var result = new StringBuilder(hash.Length * 2);
            for (int i = 0; i < hash.Length; i++) result.Append(hash[i].ToString("x2", CultureInfo.InvariantCulture));
            return result.ToString();
        }

        private static byte[] Sha256(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create()) return sha.ComputeHash(bytes);
        }

        private static bool FixedTimeEquals(byte[] left, byte[] right)
        {
            if (left == null || right == null || left.Length != right.Length) return false;
            int difference = 0;
            for (int i = 0; i < left.Length; i++) difference |= left[i] ^ right[i];
            return difference == 0;
        }

        private static int ReadInt32(Stream stream)
        {
            byte[] bytes = ReadBytes(stream, 4);
            return bytes[0] | (bytes[1] << 8) | (bytes[2] << 16) | (bytes[3] << 24);
        }

        private static void WriteInt32(Stream stream, int value)
        {
            stream.WriteByte((byte)value);
            stream.WriteByte((byte)(value >> 8));
            stream.WriteByte((byte)(value >> 16));
            stream.WriteByte((byte)(value >> 24));
        }

        private static byte[] ReadBytes(Stream stream, int length)
        {
            byte[] buffer = new byte[length];
            int offset = 0;
            while (offset < length)
            {
                int read = stream.Read(buffer, offset, length - offset);
                if (read <= 0) throw new EndOfStreamException();
                offset += read;
            }
            return buffer;
        }

        private sealed class JournalFrame
        {
            internal RawbbitStoreTransaction Transaction;
            internal string Path;
            internal long StartOffset;
        }

        private sealed class SystemRawbbitClock : IRawbbitClock
        {
            public DateTimeOffset UtcNow { get { return DateTimeOffset.UtcNow; } }
        }
    }

    internal sealed class RawbbitEnqueueOutcome
    {
        internal bool Stored;
        internal int EvictedCount;
        internal long QueueBytes;
        internal string FailureCode;
    }
}
