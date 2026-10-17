using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using Newtonsoft.Json.Linq;

namespace Rawbbit.Unity
{
    public sealed class RawbbitClient : IDisposable
    {
        private readonly object _gate = new object();
        private readonly object _dispatchGate = new object();
        private readonly object _trackScheduleGate = new object();
        private readonly RawbbitConfiguration _configuration;
        private readonly IRawbbitQueueStore _queue;
        private readonly string _installationId;
        private readonly IRawbbitClock _clock;
        private readonly IRawbbitIdentityProvider _identityProvider;
        private readonly IRawbbitSessionIdProvider _sessionIdProvider;
        private readonly RawbbitSessionStartFactory _sessionStartFactory;
        private readonly RawbbitEventContextMapper _eventContextMapper;
        private readonly SemaphoreSlim _trackSlots;
        private volatile bool _consentGranted;
        private volatile bool _withdrawalInProgress;
        private volatile bool _disposed;
        private long _consentGeneration;
        private long _locallyRejectedCount;
        private Task<RawbbitRecoveryResult> _withdrawalTask;
        private Task _lastTrackTask;
        private Task _maintenanceTask = Task.CompletedTask;
        private int _activeTrackCalls;
        private Task _activeTrackCallsIdleTask = Task.CompletedTask;
        private TaskCompletionSource<bool> _activeTrackCallsIdleSource;
        private int _adaptiveMaxEvents;
        private int _retryAttempt;
        private DateTimeOffset _retryNotBeforeUtc;
        private DateTimeOffset _nextMaintenanceUtc;
        private int _maintenanceRunning;
        private readonly Random _retryJitter = new Random();

        [ThreadStatic]
        private static RawbbitClient _clientInsideSessionFactory;

        private RawbbitClient(
            RawbbitConfiguration configuration,
            string persistentRoot,
            IRawbbitClock clock,
            IRawbbitIdentityProvider identityProvider,
            RawbbitSessionStartFactory sessionStartFactory,
            IRawbbitSessionIdProvider sessionIdProvider,
            RawbbitEventContextMapper eventContextMapper)
            : this(configuration, new RawbbitPersistentQueue(persistentRoot, configuration, clock), clock, identityProvider, sessionStartFactory, sessionIdProvider, eventContextMapper)
        {
        }

        internal RawbbitClient(
            RawbbitConfiguration configuration,
            IRawbbitQueueStore queue,
            IRawbbitClock clock = null,
            IRawbbitIdentityProvider identityProvider = null,
            RawbbitSessionStartFactory sessionStartFactory = null,
            IRawbbitSessionIdProvider sessionIdProvider = null,
            RawbbitEventContextMapper eventContextMapper = null)
        {
            if (configuration == null) throw new ArgumentNullException("configuration");
            if (queue == null) throw new ArgumentNullException("queue");
            configuration = CopyConfiguration(configuration);
            configuration.Validate();
            _configuration = configuration;
            _clock = clock ?? new SystemRawbbitClock();
            _identityProvider = identityProvider;
            _sessionIdProvider = sessionIdProvider;
            _sessionStartFactory = sessionStartFactory;
            _eventContextMapper = eventContextMapper;
            _trackSlots = new SemaphoreSlim(configuration.MaxPendingTrackOperations, configuration.MaxPendingTrackOperations);
            _adaptiveMaxEvents = configuration.MaxEventsPerRequest;
            _queue = queue;
            _installationId = queue.InstallationId;
        }

        internal static Task<RawbbitClient> OpenAsync(
            RawbbitConfiguration configuration,
            string persistentRoot = null,
            IRawbbitClock clock = null,
            IRawbbitIdentityProvider identityProvider = null,
            RawbbitSessionStartFactory sessionStartFactory = null,
            IRawbbitSessionIdProvider sessionIdProvider = null,
            RawbbitEventContextMapper eventContextMapper = null)
        {
            if (configuration == null) throw new ArgumentNullException("configuration");
            RawbbitConfiguration copy = CopyConfiguration(configuration);
            string root = persistentRoot ?? Application.persistentDataPath;
            if (String.IsNullOrWhiteSpace(root)) throw new ArgumentException("Persistent storage root is unavailable.");
            copy.Validate();
            return Task.Run(() => new RawbbitClient(copy, root, clock, identityProvider, sessionStartFactory, sessionIdProvider, eventContextMapper));
        }

        public bool HasCurrentConsent { get { return _consentGranted && !_withdrawalInProgress && !_disposed; } }

        public Task<RawbbitRecoveryResult> ConfirmConsentAsync()
        {
            if (_disposed) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_disposed" });
            if (_withdrawalInProgress) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "withdrawal_in_progress" });
            long expectedGeneration = Interlocked.Read(ref _consentGeneration);
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (_disposed || _withdrawalInProgress)
                        return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_unavailable" };
                    if (_consentGranted) return new RawbbitRecoveryResult { Succeeded = true, SafeDiagnosticCode = "consent_already_confirmed" };
                    if (expectedGeneration != Interlocked.Read(ref _consentGeneration))
                        return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_unavailable" };
                    try
                    {
                        _queue.GrantConsent();
                        lock (_dispatchGate)
                        {
                            if (_disposed || _withdrawalInProgress || expectedGeneration != Interlocked.Read(ref _consentGeneration) ||
                                Interlocked.CompareExchange(ref _consentGeneration, expectedGeneration + 1, expectedGeneration) != expectedGeneration)
                                return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_changed_during_confirmation" };
                            _consentGranted = true;
                            if (_disposed || _withdrawalInProgress || Interlocked.Read(ref _consentGeneration) != expectedGeneration + 1)
                            {
                                _consentGranted = false;
                                return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_changed_during_confirmation" };
                            }
                        }
                        _retryNotBeforeUtc = DateTimeOffset.MinValue;
                        return new RawbbitRecoveryResult { Succeeded = true, SafeDiagnosticCode = "consent_confirmed" };
                    }
                    catch
                    {
                        _consentGranted = false;
                        return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_state_persistence_failed" };
                    }
                }
            });
        }

        public Task<RawbbitRecoveryResult> WithdrawConsentAsync()
        {
            lock (_dispatchGate)
            {
                if (_disposed) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_disposed" });
                if (_withdrawalInProgress && _withdrawalTask != null && !_withdrawalTask.IsCompleted) return _withdrawalTask;
                _consentGranted = false;
                _withdrawalInProgress = true;
                long withdrawalGeneration = Interlocked.Increment(ref _consentGeneration);
                _withdrawalTask = Task.Run(() =>
                {
                    lock (_gate)
                    {
                        try
                        {
                            _queue.WithdrawAndPurge(_clock.UtcNow);
                            _retryAttempt = 0;
                            _retryNotBeforeUtc = DateTimeOffset.MaxValue;
                            lock (_dispatchGate)
                            {
                                if (Interlocked.Read(ref _consentGeneration) == withdrawalGeneration)
                                    _withdrawalInProgress = false;
                            }
                            return new RawbbitRecoveryResult { Succeeded = true, SafeDiagnosticCode = "withdrawal_purged" };
                        }
                        catch
                        {
                            // Fail closed. The caller must surface the purge failure
                            // and must not grant consent until storage is recovered.
                            lock (_dispatchGate)
                            {
                                if (Interlocked.Read(ref _consentGeneration) == withdrawalGeneration)
                                    _withdrawalInProgress = true;
                            }
                            return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "withdrawal_purge_failed" };
                        }
                    }
                });
                return _withdrawalTask;
            }
        }

        public Task<RawbbitTrackResult> TrackAsync(RawbbitEventInput input)
        {
            if (System.Object.ReferenceEquals(_clientInsideSessionFactory, this))
                return Task.FromResult(RejectedTrackResult(null, "session_factory_reentrancy_rejected"));
            long consentGeneration;
            lock (_dispatchGate)
            {
                if (_disposed) return Task.FromResult(TrackResult(RawbbitTrackStatus.ConsentWithdrawn, null, "client_disposed"));
                if (!_consentGranted || _withdrawalInProgress)
                    return Task.FromResult(TrackResult(_withdrawalInProgress ? RawbbitTrackStatus.ConsentWithdrawn : RawbbitTrackStatus.ConsentUnknown, null, "consent_required"));
                if (!_trackSlots.Wait(0)) return Task.FromResult(TrackResult(RawbbitTrackStatus.Busy, null, "tracking_capacity_reached"));
                if (_activeTrackCalls++ == 0)
                {
                    _activeTrackCallsIdleSource = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                    _activeTrackCallsIdleTask = _activeTrackCallsIdleSource.Task;
                }
                consentGeneration = Interlocked.Read(ref _consentGeneration);
            }

            DateTimeOffset trackingTime;
            try { trackingTime = _clock.UtcNow.ToUniversalTime(); }
            catch
            {
                _trackSlots.Release();
                FinishTrackCall();
                return Task.FromResult(RejectedTrackResult(null, "tracking_clock_unavailable"));
            }
            Task previousTrack;
            var trackCompletion = new TaskCompletionSource<RawbbitTrackResult>(TaskCreationOptions.RunContinuationsAsynchronously);
            lock (_trackScheduleGate)
            {
                previousTrack = _lastTrackTask;
                _lastTrackTask = trackCompletion.Task;
            }

            RawbbitEventInput snapshot;
            try
            {
                snapshot = RawbbitPayload.CloneInput(input);
                if (snapshot == null) throw new ArgumentException("Event is required.");
                RawbbitPayload.ValidateInput(snapshot, _configuration.MaxRequestBytes);
                snapshot.EventId = NormalizeOrGenerateEventId(snapshot.EventId);
                snapshot.EventTimestamp = RawbbitPayload.FormatTimestampForSdk(snapshot.EventTimestamp, trackingTime);
                if (String.IsNullOrWhiteSpace(snapshot.UserPseudoId)) snapshot.UserPseudoId = _identityProvider == null ? _installationId : _identityProvider.GetUserPseudoId();
                if (snapshot.UserId == null && _identityProvider != null) snapshot.UserId = _identityProvider.GetUserId();
                // Force JSON validation/deep conversion before returning control
                // to game code, so subsequent mutation cannot change the event.
                RawbbitPayload.ValidateInput(snapshot, _configuration.MaxRequestBytes);
            }
            catch
            {
                RawbbitTrackResult invalid = RejectedTrackResult(null, "invalid_event_input");
                if (previousTrack == null)
                {
                    trackCompletion.TrySetResult(invalid);
                    _trackSlots.Release();
                    FinishTrackCall();
                }
                else Task.Run(async () =>
                {
                    try { await previousTrack.ConfigureAwait(false); }
                    catch { }
                    trackCompletion.TrySetResult(invalid);
                    _trackSlots.Release();
                    FinishTrackCall();
                });
                return trackCompletion.Task;
            }

            Task.Run(async () =>
            {
                try
                {
                    if (previousTrack != null)
                    {
                        try { await previousTrack.ConfigureAwait(false); }
                        catch { }
                    }
                    trackCompletion.TrySetResult(ProcessTrackSnapshot(snapshot, trackingTime, consentGeneration));
                }
                catch
                {
                    trackCompletion.TrySetResult(TrackResult(RawbbitTrackStatus.InvalidEvent, snapshot == null ? null : snapshot.EventId, "event_processing_failed"));
                }
                finally
                {
                    _trackSlots.Release();
                    FinishTrackCall();
                }
            });
            return trackCompletion.Task;
        }

        private RawbbitTrackResult ProcessTrackSnapshot(RawbbitEventInput snapshot, DateTimeOffset trackingTime, long consentGeneration)
        {
            try
            {
                lock (_gate)
                {
                    if (_disposed) return TrackResult(RawbbitTrackStatus.ConsentWithdrawn, null, "client_disposed");
                    if (!_consentGranted || _withdrawalInProgress || consentGeneration != Interlocked.Read(ref _consentGeneration))
                        return TrackResult(_withdrawalInProgress ? RawbbitTrackStatus.ConsentWithdrawn : RawbbitTrackStatus.ConsentUnknown, null, "consent_changed_before_persistence");
                    if (_queue.IsStorageFaulted) return TrackResult(RawbbitTrackStatus.StorageFailure, null, "queue_storage_faulted");

                    DateTimeOffset now = trackingTime;
                    RawbbitSessionState previous = _queue.ReadSession();
                    Func<string> sessionIdFactory = _sessionIdProvider == null ? null : new Func<string>(_sessionIdProvider.CreateSessionId);
                    SessionDecision decision = RawbbitIdentitySession.Decide(previous, now, TimeSpan.FromMinutes(_configuration.SessionTimeoutMinutes), false, sessionIdFactory);
                    string triggerEventId = NormalizeOrGenerateEventId(snapshot.EventId);
                    snapshot.EventId = triggerEventId;
                    snapshot.EventTimestamp = RawbbitPayload.FormatTimestampForSdk(snapshot.EventTimestamp, now);

                    RawbbitEventInput sessionStartedInput = null;
                    bool expectSessionStartedEvent = decision.IsNewSession && _sessionStartFactory != null;
                    if (expectSessionStartedEvent)
                    {
                        decision.TriggerSequence = 2;
                        decision.State.EventCounter = 2;
                    }
                    RawbbitSessionContext context = new RawbbitSessionContext
                    {
                        SessionId = decision.State.SessionId,
                        IsNewSession = decision.IsNewSession,
                        SessionStartedEventSequence = decision.IsNewSession && expectSessionStartedEvent ? 1 : 0,
                        TriggeringEventSequence = decision.TriggerSequence,
                        EventSequence = decision.TriggerSequence,
                        EventTimestamp = snapshot.EventTimestamp
                    };
                    if (decision.IsNewSession && _sessionStartFactory != null)
                    {
                        RawbbitClient previousFactoryClient = _clientInsideSessionFactory;
                        _clientInsideSessionFactory = this;
                        try { sessionStartedInput = _sessionStartFactory(context, RawbbitPayload.CloneInput(snapshot)); }
                        finally { _clientInsideSessionFactory = previousFactoryClient; }
                        if (sessionStartedInput != null)
                        {
                            sessionStartedInput = RawbbitPayload.CloneInput(sessionStartedInput);
                            if (String.IsNullOrWhiteSpace(sessionStartedInput.EventId)) sessionStartedInput.EventId = Guid.NewGuid().ToString("D");
                            else sessionStartedInput.EventId = NormalizeOrGenerateEventId(sessionStartedInput.EventId);
                            if (sessionStartedInput.EventId == triggerEventId)
                                return RejectedTrackResult(triggerEventId, "duplicate_session_pair_event_id");
                            if (String.IsNullOrWhiteSpace(sessionStartedInput.EventTimestamp)) sessionStartedInput.EventTimestamp = snapshot.EventTimestamp;
                            if (sessionStartedInput.UserId == null) sessionStartedInput.UserId = snapshot.UserId;
                            if (String.IsNullOrWhiteSpace(sessionStartedInput.UserPseudoId)) sessionStartedInput.UserPseudoId = snapshot.UserPseudoId;
                            RawbbitPayload.ValidateInput(sessionStartedInput, _configuration.MaxRequestBytes);
                        }
                        else
                        {
                            decision.TriggerSequence = 1;
                            decision.State.EventCounter = 1;
                            context.SessionStartedEventSequence = 0;
                            context.TriggeringEventSequence = 1;
                            context.EventSequence = 1;
                        }
                    }

                    if (_eventContextMapper != null)
                    {
                        RawbbitClient previousMapperClient = _clientInsideSessionFactory;
                        _clientInsideSessionFactory = this;
                        try { _eventContextMapper(context, snapshot); }
                        finally { _clientInsideSessionFactory = previousMapperClient; }
                        RawbbitPayload.ValidateInput(snapshot, _configuration.MaxRequestBytes);
                    }

                    var records = new List<RawbbitQueueRecord>();
                    if (sessionStartedInput != null)
                    {
                        string sessionJson = RawbbitPayload.BuildEventJson(sessionStartedInput, _configuration, _installationId, decision.State.SessionId, sessionStartedInput.EventId, now);
                        records.Add(CreateRecord(sessionStartedInput.EventId, sessionJson, now));
                    }
                    string triggerJson = RawbbitPayload.BuildEventJson(snapshot, _configuration, _installationId, decision.State.SessionId, triggerEventId, now);
                    records.Add(CreateRecord(triggerEventId, triggerJson, now));

                    RawbbitEnqueueOutcome outcome = _queue.Enqueue(records, decision.State, now);
                    if (!outcome.Stored) return RejectedTrackResult(triggerEventId, outcome.FailureCode ?? "queue_capacity_rejected");
                    return TrackResult(RawbbitTrackStatus.DurablyStored, triggerEventId,
                        outcome.EvictedCount > 0 ? "durably_stored_oldest_events_evicted" : "durably_stored");
                }
            }
            catch
            {
                if (!_queue.IsStorageFaulted) Interlocked.Increment(ref _locallyRejectedCount);
                return TrackResult(_queue.IsStorageFaulted ? RawbbitTrackStatus.StorageFailure : RawbbitTrackStatus.InvalidEvent,
                    snapshot == null ? null : snapshot.EventId,
                    _queue.IsStorageFaulted ? "queue_storage_failure" : "event_build_failure");
            }
        }

        public RawbbitQueueDiagnostics GetDiagnostics()
        {
            RawbbitQueueDiagnostics diagnostics = _queue.ReadDiagnostics();
            long rejected = Interlocked.Read(ref _locallyRejectedCount);
            diagnostics.LocallyRejectedCount = rejected > Int32.MaxValue ? Int32.MaxValue : (int)rejected;
            return diagnostics;
        }

        public IList<RawbbitQuarantinedRecordInfo> ListQuarantinedRecords()
        {
            return _queue.ListQuarantined();
        }

        public Task<RawbbitRecoveryResult> DiscardQuarantinedAsync(IList<string> opaqueHandles, long expectedFaultRevision, long expectedQueueGeneration, string reasonCode, string policyVersion)
        {
            if (!HasCurrentConsent) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "fresh_consent_required" });
            List<string> handles = opaqueHandles == null ? null : opaqueHandles.ToList();
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (!HasCurrentConsent) return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_changed" };
                    try { return _queue.DiscardQuarantined(handles, expectedFaultRevision, expectedQueueGeneration, reasonCode, policyVersion, _clock.UtcNow); }
                    catch { return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_storage_failure" }; }
                }
            });
        }

        public Task<RawbbitRecoveryResult> RetryQuarantinedAsync(IList<string> opaqueHandles, long expectedFaultRevision, long expectedQueueGeneration, string reasonCode, string policyVersion)
        {
            if (!HasCurrentConsent) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "fresh_consent_required" });
            List<string> handles = opaqueHandles == null ? null : opaqueHandles.ToList();
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (!HasCurrentConsent) return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_changed" };
                    try { return _queue.RetryQuarantined(handles, expectedFaultRevision, expectedQueueGeneration, reasonCode, policyVersion, _clock.UtcNow); }
                    catch { return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_storage_failure" }; }
                }
            });
        }

        public Task<RawbbitRecoveryResult> ValidateAndResumeAsync(long expectedFaultRevision, long expectedQueueGeneration, string reasonCode, string policyVersion)
        {
            if (!HasCurrentConsent) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "fresh_consent_required" });
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (!HasCurrentConsent) return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_changed" };
                    try { return _queue.ValidateAndResume(expectedFaultRevision, expectedQueueGeneration, reasonCode, policyVersion, _clock.UtcNow); }
                    catch { return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_storage_failure" }; }
                }
            });
        }

        internal Task<RawbbitRecoveryProbePlan> PrepareRecoveryProbeAsync(long expectedFaultRevision, long expectedQueueGeneration)
        {
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (!HasCurrentConsent)
                        return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "fresh_consent_required" };
                    try
                    {
                        DateTimeOffset now = _clock.UtcNow;
                        _queue.PruneExpired(now);
                        RawbbitFaultState fault = _queue.ReadFault();
                        RawbbitQueueDiagnostics diagnostics = _queue.ReadDiagnostics();
                        if (fault == null) return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "delivery_fault_not_active" };
                        if (fault.Revision != expectedFaultRevision || diagnostics.QueueGeneration != expectedQueueGeneration)
                            return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "stale_recovery_revision" };
                        if (!fault.RequiresProbe || fault.ProbeValidated)
                            return new RawbbitRecoveryProbePlan { Succeeded = true, SafeDiagnosticCode = fault.ProbeValidated ? "recovery_probe_already_validated" : "recovery_probe_not_required" };
                        if (diagnostics.QuarantinedCount > 0)
                            return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "quarantined_records_remain" };

                        RawbbitQueueRecord record = _queue.GetRecoveryProbeRecord(now);
                        if (record == null)
                            return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "recovery_probe_pending_event_required" };
                        if (!RawbbitPayload.ValidateFrozenEvent(record.PayloadJson, _configuration.AppId, _configuration.Environment, _configuration.Platform, _configuration.MaxRequestBytes))
                        {
                            _queue.Quarantine(new List<string> { record.Handle }, 0, "recovery_probe_event_invalid", now, true);
                            return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "recovery_probe_event_quarantined_for_review" };
                        }
                        var records = new List<RawbbitQueueRecord> { record };
                        return new RawbbitRecoveryProbePlan
                        {
                            Succeeded = true,
                            SafeDiagnosticCode = "recovery_probe_ready",
                            Batch = new RawbbitPreparedBatch
                            {
                                Records = records,
                                Handles = new List<string> { record.Handle },
                                Body = RawbbitPayload.BuildBatchBody(records, _configuration),
                                Endpoint = RawbbitPayload.NormalizeEndpoint(_configuration.CollectorUrl),
                                ApiKey = _configuration.ApiKey,
                                TimeoutSeconds = _configuration.RequestTimeoutSeconds,
                                ConsentGeneration = Interlocked.Read(ref _consentGeneration),
                                RecoveryFaultRevision = fault.Revision,
                                RecoveryQueueGeneration = diagnostics.QueueGeneration,
                                IsRecoveryProbe = true
                            }
                        };
                    }
                    catch
                    {
                        return new RawbbitRecoveryProbePlan { Succeeded = false, SafeDiagnosticCode = "recovery_probe_preparation_failed" };
                    }
                }
            });
        }

        internal Task<RawbbitRecoveryResult> CompleteRecoveryProbeAsync(RawbbitPreparedBatch batch, RawbbitTransportResult response)
        {
            if (batch == null || !batch.IsRecoveryProbe || response == null)
                return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "invalid_recovery_probe" });
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (!HasCurrentConsent || batch.ConsentGeneration != Interlocked.Read(ref _consentGeneration))
                        return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "consent_changed" };
                    try
                    {
                        RawbbitFaultState fault = _queue.ReadFault();
                        RawbbitQueueDiagnostics diagnostics = _queue.ReadDiagnostics();
                        if (fault == null || fault.Revision != batch.RecoveryFaultRevision ||
                            diagnostics.QueueGeneration != batch.RecoveryQueueGeneration)
                            return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "stale_recovery_revision" };

                        if (response.StatusCode >= 200 && response.StatusCode < 300 && !response.NetworkError)
                        {
                            int accepted;
                            if (!TryReadAcceptedCount(response.ResponseBody, out accepted) || accepted != 1)
                            {
                                _queue.LatchFault("recovery_probe_acceptance_mismatch", response.StatusCode, _clock.UtcNow, true);
                                return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_probe_acceptance_mismatch" };
                            }
                            _queue.MarkRecoveryProbeValidated(batch.RecoveryFaultRevision, batch.RecoveryQueueGeneration, batch.Handles[0], _clock.UtcNow);
                            RawbbitQueueDiagnostics updated = _queue.ReadDiagnostics();
                            return new RawbbitRecoveryResult
                            {
                                Succeeded = true,
                                SafeDiagnosticCode = "recovery_probe_validated",
                                FaultRevision = updated.FaultRevision,
                                QueueGeneration = updated.QueueGeneration
                            };
                        }

                        if (!response.NetworkError && (response.StatusCode == 400 || response.StatusCode == 413 || response.StatusCode == 422))
                            _queue.Quarantine(batch.Handles, response.StatusCode, "recovery_probe_rejected", _clock.UtcNow, true);
                        else if (!response.NetworkError && response.StatusCode != 408 && response.StatusCode != 429 && response.StatusCode < 500)
                            _queue.LatchFault("recovery_probe_server_rejected", response.StatusCode, _clock.UtcNow, true);
                        return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_probe_failed" };
                    }
                    catch
                    {
                        return new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_probe_persistence_failed" };
                    }
                }
            });
        }

        internal RawbbitPreparedBatch TryBeginRequest(DateTimeOffset now)
        {
            try
            {
                lock (_gate)
                {
                    if (!HasCurrentConsent || _queue.IsStorageFaulted || _queue.ReadFault() != null || now < _retryNotBeforeUtc) return null;
                    _queue.PruneExpired(now);
                    IList<RawbbitQueueRecord> records = _queue.GetPendingRecords(Math.Min(_configuration.MaxEventsPerRequest, _adaptiveMaxEvents), _configuration.MaxRequestBytes, now);
                    if (records.Count == 0)
                    {
                        RawbbitQueueRecord oldest = _queue.GetOldestPendingRecord(now);
                        if (oldest == null) return null;
                        return new RawbbitPreparedBatch
                        {
                            Records = new List<RawbbitQueueRecord> { oldest },
                            Handles = new List<string> { oldest.Handle },
                            ConsentGeneration = Interlocked.Read(ref _consentGeneration),
                            RequiresLocalQuarantine = true
                        };
                    }
                    List<string> invalidHandles = records
                        .Where(record => !RawbbitPayload.ValidateFrozenEvent(record.PayloadJson, _configuration.AppId, _configuration.Environment, _configuration.Platform, _configuration.MaxRequestBytes))
                        .Select(record => record.Handle)
                        .ToList();
                    if (invalidHandles.Count > 0)
                    {
                        _queue.Quarantine(invalidHandles, 0, "local_frozen_event_validation_failed", now, true);
                        return null;
                    }
                    string body = RawbbitPayload.BuildBatchBody(records, _configuration);
                    return new RawbbitPreparedBatch
                    {
                        Records = records,
                        Handles = records.Select(record => record.Handle).ToList(),
                        Body = body,
                        Endpoint = RawbbitPayload.NormalizeEndpoint(_configuration.CollectorUrl),
                        ApiKey = _configuration.ApiKey,
                        TimeoutSeconds = _configuration.RequestTimeoutSeconds,
                        ConsentGeneration = Interlocked.Read(ref _consentGeneration),
                        Attempt = _retryAttempt
                    };
                }
            }
            catch
            {
                if (!_queue.IsStorageFaulted)
                {
                    try { _queue.LatchFault("queue_prepare_failed", 0, _clock.UtcNow, false); }
                    catch { }
                }
                return null;
            }
        }

        internal RawbbitPreparedBatch TryBeginRequest()
        {
            return TryBeginRequest(_clock.UtcNow);
        }

        internal void PumpMaintenance(DateTimeOffset now)
        {
            if (_disposed || now < _nextMaintenanceUtc || Interlocked.CompareExchange(ref _maintenanceRunning, 1, 0) != 0) return;
            _nextMaintenanceUtc = AddUtcSecondsClamped(now, 60);
            _maintenanceTask = Task.Run(() =>
            {
                try
                {
                    lock (_gate)
                    {
                        if (!_disposed) _queue.PruneExpired(_clock.UtcNow);
                    }
                }
                catch { }
                finally { Interlocked.Exchange(ref _maintenanceRunning, 0); }
            });
        }

        internal void PumpMaintenance()
        {
            PumpMaintenance(_clock.UtcNow);
        }

        internal bool TryStartDispatch(RawbbitPreparedBatch batch, Func<Task<RawbbitTransportResult>> startRequest, out Task<RawbbitTransportResult> requestTask)
        {
            requestTask = null;
            if (batch == null || startRequest == null) return false;
            // Dispatch runs on Unity's main thread; defer rather than waiting
            // for a worker's disk commit or adapter callback.
            if (!Monitor.TryEnter(_gate)) return false;
            try
            {
                lock (_dispatchGate)
                {
                    if (!HasCurrentConsent || batch.ConsentGeneration != Interlocked.Read(ref _consentGeneration)) return false;
                    try
                    {
                        Task<RawbbitTransportResult> started = null;
                        bool dispatched = _queue.TryDispatch(batch, () =>
                        {
                            started = startRequest();
                            return started != null;
                        });
                        requestTask = started;
                        return dispatched;
                    }
                    catch
                    {
                        return false;
                    }
                }
            }
            finally { Monitor.Exit(_gate); }
        }

        internal Task CompleteRequestAsync(RawbbitPreparedBatch batch, RawbbitTransportResult response)
        {
            return Task.Run(() =>
            {
                lock (_gate)
                {
                    if (batch == null || response == null || batch.ConsentGeneration != Interlocked.Read(ref _consentGeneration) || !HasCurrentConsent) return;
                    DateTimeOffset now = _clock.UtcNow;
                    try
                    {
                        if (batch.RequiresLocalQuarantine)
                        {
                            _queue.Quarantine(batch.Handles, 0, "single_event_exceeds_adaptive_limit", now, false);
                            return;
                        }
                        if (response.StatusCode >= 200 && response.StatusCode < 300 && !response.NetworkError)
                        {
                            int accepted;
                            if (TryReadAcceptedCount(response.ResponseBody, out accepted) && accepted == batch.Records.Count)
                            {
                                _queue.Acknowledge(batch.Handles);
                                _retryAttempt = 0;
                                _retryNotBeforeUtc = DateTimeOffset.MinValue;
                            }
                            else
                            {
                                _queue.LatchFault("accepted_count_mismatch", response.StatusCode, now, true);
                            }
                            return;
                        }

                        if (response.StatusCode == 413 && !response.NetworkError)
                        {
                            if (batch.Records.Count > 1)
                            {
                                _adaptiveMaxEvents = Math.Max(1, Math.Min(_adaptiveMaxEvents - 1, batch.Records.Count / 2));
                                _retryNotBeforeUtc = now;
                            }
                            else
                            {
                                _queue.Quarantine(batch.Handles, 413, "single_event_http_413", now, true);
                            }
                            return;
                        }

                        if ((response.StatusCode == 400 || response.StatusCode == 422) && !response.NetworkError)
                        {
                            _queue.Quarantine(batch.Handles, response.StatusCode, response.StatusCode == 400 ? "collector_http_400" : "collector_http_422", now, true);
                            return;
                        }

                        if ((response.StatusCode == 401 || response.StatusCode == 403 || response.StatusCode == 404) && !response.NetworkError)
                        {
                            _queue.LatchFault("collector_auth_or_route_failure", response.StatusCode, now, true);
                            return;
                        }

                        if (response.NetworkError || response.StatusCode == 408 || response.StatusCode == 429 || response.StatusCode >= 500)
                        {
                            ScheduleRetry(now, response.RetryAfterSeconds);
                            return;
                        }

                        _queue.LatchFault("collector_unexpected_response", response.StatusCode, now, true);
                    }
                    catch
                    {
                        if (!_queue.IsStorageFaulted)
                        {
                            try { _queue.LatchFault("response_persistence_failure", response.StatusCode, now, true); }
                            catch { }
                        }
                    }
                }
            });
        }

        internal Task DisposeClient()
        {
            lock (_dispatchGate)
            {
                _disposed = true;
                _consentGranted = false;
                Interlocked.Increment(ref _consentGeneration);
                Task gateDrain = Task.Run(() =>
                {
                    lock (_gate) { }
                });
                var drains = new List<Task> { _activeTrackCallsIdleTask, _maintenanceTask, gateDrain };
                if (_withdrawalTask != null) drains.Add(_withdrawalTask);
                return Task.WhenAll(drains.ToArray());
            }
        }

        public void Dispose()
        {
            DisposeClient();
        }

        private void ScheduleRetry(DateTimeOffset now, int? retryAfterSeconds)
        {
            if (_retryAttempt < _configuration.MaxImmediateRetries)
            {
                int exponent = Math.Min(_retryAttempt, 8);
                double baseDelay = Math.Min(30.0, 0.25 * Math.Pow(2, exponent));
                double delay = Math.Min(30.0, baseDelay * (0.8 + (_retryJitter.NextDouble() * 0.4)));
                _retryAttempt++;
                if (retryAfterSeconds.HasValue) delay = Math.Max(delay, Math.Min(3600, Math.Max(0, retryAfterSeconds.Value)));
                _retryNotBeforeUtc = AddUtcSecondsClamped(now, delay);
            }
            else
            {
                _retryAttempt = 0;
                int cooldown = _configuration.RetryCooldownSeconds;
                if (retryAfterSeconds.HasValue) cooldown = Math.Max(cooldown, Math.Min(3600, Math.Max(0, retryAfterSeconds.Value)));
                _retryNotBeforeUtc = AddUtcSecondsClamped(now, cooldown);
            }
        }

        private static DateTimeOffset AddUtcSecondsClamped(DateTimeOffset value, double seconds)
        {
            DateTime utc = value.UtcDateTime;
            long availableTicks = DateTime.MaxValue.Ticks - utc.Ticks;
            double requestedTicks = Math.Max(0.0, seconds) * TimeSpan.TicksPerSecond;
            long ticksToAdd = requestedTicks >= availableTicks ? availableTicks : (long)requestedTicks;
            return new DateTimeOffset(utc.AddTicks(ticksToAdd), TimeSpan.Zero);
        }

        private static bool TryReadAcceptedCount(string responseBody, out int accepted)
        {
            accepted = -1;
            if (String.IsNullOrEmpty(responseBody) || responseBody.Length > 4096) return false;
            try
            {
                JObject value = JObject.Parse(responseBody);
                JToken count = value["accepted_events"];
                return count != null && count.Type == JTokenType.Integer && Int32.TryParse(count.ToString(), NumberStyles.None, CultureInfo.InvariantCulture, out accepted);
            }
            catch { return false; }
        }

        private static RawbbitQueueRecord CreateRecord(string eventId, string payload, DateTimeOffset now)
        {
            var record = new RawbbitQueueRecord
            {
                Handle = Guid.NewGuid().ToString("N"),
                EventId = eventId,
                PayloadJson = payload,
                EnqueuedUtcTicks = now.UtcDateTime.Ticks,
                Quarantined = false,
                Sequence = 0
            };
            record.QueueBytes = RawbbitPayload.MeasureRecordBytes(record);
            return record;
        }

        private static string NormalizeOrGenerateEventId(string value)
        {
            if (String.IsNullOrWhiteSpace(value)) return Guid.NewGuid().ToString("D");
            Guid parsed;
            if (!Guid.TryParse(value, out parsed)) throw new ArgumentException("EventId must be a UUID.");
            return parsed.ToString("D");
        }

        private static RawbbitTrackResult TrackResult(RawbbitTrackStatus status, string eventId, string diagnostic)
        {
            return new RawbbitTrackResult { Status = status, EventId = eventId, SafeDiagnosticCode = diagnostic };
        }

        private RawbbitTrackResult RejectedTrackResult(string eventId, string diagnostic)
        {
            Interlocked.Increment(ref _locallyRejectedCount);
            return TrackResult(RawbbitTrackStatus.InvalidEvent, eventId, diagnostic);
        }

        private void FinishTrackCall()
        {
            TaskCompletionSource<bool> idle = null;
            lock (_dispatchGate)
            {
                if (_activeTrackCalls <= 0) return;
                _activeTrackCalls--;
                if (_activeTrackCalls == 0)
                {
                    idle = _activeTrackCallsIdleSource;
                    _activeTrackCallsIdleSource = null;
                    _activeTrackCallsIdleTask = Task.CompletedTask;
                }
            }
            if (idle != null) idle.TrySetResult(true);
        }

        private static RawbbitConfiguration CopyConfiguration(RawbbitConfiguration source)
        {
            return new RawbbitConfiguration
            {
                CollectorUrl = source.CollectorUrl,
                PreviousCollectorUrl = source.PreviousCollectorUrl,
                AllowFreshStoreWithUnresolvedLegacyStores = source.AllowFreshStoreWithUnresolvedLegacyStores,
                ApiKey = source.ApiKey,
                AppId = source.AppId,
                Environment = source.Environment,
                Product = source.Product,
                Surface = source.Surface,
                SourceType = source.SourceType,
                SchemaVersion = source.SchemaVersion,
                Platform = source.Platform,
                DefaultDevice = source.DefaultDevice == null ? new Dictionary<string, object>() : RawbbitPayload.CloneMap(source.DefaultDevice),
                MaxQueueBytes = source.MaxQueueBytes,
                RetentionDays = source.RetentionDays,
                SessionTimeoutMinutes = source.SessionTimeoutMinutes,
                MaxEventsPerRequest = source.MaxEventsPerRequest,
                MaxRequestBytes = source.MaxRequestBytes,
                RequestTimeoutSeconds = source.RequestTimeoutSeconds,
                MaxImmediateRetries = source.MaxImmediateRetries,
                RetryCooldownSeconds = source.RetryCooldownSeconds,
                JournalCompactionBytes = source.JournalCompactionBytes,
                MaxPendingTrackOperations = source.MaxPendingTrackOperations,
                AllowInsecureHttpForDevelopment = source.AllowInsecureHttpForDevelopment
            };
        }

        private sealed class SystemRawbbitClock : IRawbbitClock
        {
            public DateTimeOffset UtcNow { get { return DateTimeOffset.UtcNow; } }
        }
    }

    internal sealed class RawbbitPreparedBatch
    {
        internal IList<RawbbitQueueRecord> Records;
        internal IList<string> Handles;
        internal string Body;
        internal string Endpoint;
        internal string ApiKey;
        internal int TimeoutSeconds;
        internal long ConsentGeneration;
        internal int Attempt;
        internal bool RequiresLocalQuarantine;
        internal bool IsRecoveryProbe;
        internal long RecoveryFaultRevision;
        internal long RecoveryQueueGeneration;
    }

    internal sealed class RawbbitRecoveryProbePlan
    {
        internal bool Succeeded;
        internal string SafeDiagnosticCode;
        internal RawbbitPreparedBatch Batch;
    }

    internal sealed class RawbbitTransportResult
    {
        internal int StatusCode;
        internal bool NetworkError;
        internal string ResponseBody;
        internal int? RetryAfterSeconds;
    }
}
