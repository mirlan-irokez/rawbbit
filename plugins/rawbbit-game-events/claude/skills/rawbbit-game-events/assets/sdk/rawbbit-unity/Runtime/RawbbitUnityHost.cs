using System;
using System.Collections;
using System.Collections.Generic;
using System.Threading.Tasks;
using UnityEngine;

namespace Rawbbit.Unity
{
    [DisallowMultipleComponent]
    public sealed class RawbbitUnityHost : MonoBehaviour
    {
        private static volatile RawbbitUnityHost _instance;
        private Task<RawbbitClient> _openTask;
        private Task<RawbbitPreparedBatch> _prepareTask;
        private IRawbbitTransport _transport = new RawbbitTransport();
        private bool _sending;
        private volatile bool _destroyed;
        private bool _recoveryRunning;
        private TaskCompletionSource<RawbbitRecoveryResult> _activeRecoveryCompletion;
        private float _nextPrepareRealtime;

        public RawbbitClient Client { get; private set; }
        public bool IsReady { get { return Client != null; } }
        public bool InitializationFailed { get; private set; }

        public static RawbbitUnityHost Create(
            RawbbitConfiguration configuration,
            IRawbbitIdentityProvider identityProvider = null,
            RawbbitSessionStartFactory sessionStartFactory = null,
            IRawbbitSessionIdProvider sessionIdProvider = null,
            RawbbitEventContextMapper eventContextMapper = null)
        {
            return CreateInternal(configuration, Application.persistentDataPath, identityProvider, sessionStartFactory, sessionIdProvider, eventContextMapper, null, null);
        }

        internal static RawbbitUnityHost CreateForTests(
            RawbbitConfiguration configuration,
            string persistentRoot,
            IRawbbitTransport transport,
            IRawbbitClock clock = null,
            IRawbbitIdentityProvider identityProvider = null,
            RawbbitSessionStartFactory sessionStartFactory = null,
            IRawbbitSessionIdProvider sessionIdProvider = null,
            RawbbitEventContextMapper eventContextMapper = null)
        {
            if (transport == null) throw new ArgumentNullException("transport");
            return CreateInternal(configuration, persistentRoot, identityProvider, sessionStartFactory, sessionIdProvider, eventContextMapper, clock, transport);
        }

        private static RawbbitUnityHost CreateInternal(
            RawbbitConfiguration configuration,
            string persistentRoot,
            IRawbbitIdentityProvider identityProvider,
            RawbbitSessionStartFactory sessionStartFactory,
            IRawbbitSessionIdProvider sessionIdProvider,
            RawbbitEventContextMapper eventContextMapper,
            IRawbbitClock clock,
            IRawbbitTransport transport)
        {
            if (configuration == null) throw new ArgumentNullException("configuration");
            if (!System.Object.ReferenceEquals(_instance, null)) throw new InvalidOperationException("Only one RawbbitUnityHost may be active in a Unity player.");

            RawbbitConfiguration config = CopyConfiguration(configuration);
            config.Platform = ResolvePlatform(Application.platform);
            AddDeviceDefault(config.DefaultDevice, "app_version", Application.version);
            AddDeviceDefault(config.DefaultDevice, "os_version", SystemInfo.operatingSystem);
            AddDeviceDefault(config.DefaultDevice, "device_model", SystemInfo.deviceModel);
            config.Validate();
            var hostObject = new GameObject("RawbbitUnityHost");
            DontDestroyOnLoad(hostObject);
            RawbbitUnityHost host = hostObject.AddComponent<RawbbitUnityHost>();
            _instance = host;
            if (transport != null) host._transport = transport;
            host._openTask = RawbbitClient.OpenAsync(config, persistentRoot, clock, identityProvider, sessionStartFactory, sessionIdProvider, eventContextMapper);
            host._openTask.ContinueWith(task =>
            {
                if (!host._destroyed) return;
                Task drain = task.Status == TaskStatus.RanToCompletion ? task.Result.DisposeClient() : Task.CompletedTask;
                ReleaseHostWhenComplete(host, drain);
            }, TaskScheduler.Default);
            host.StartCoroutine(host.CompleteInitialization());
            return host;
        }

        public Task<RawbbitRecoveryResult> ConfirmConsentAsync()
        {
            if (Client == null) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_not_ready" });
            return Client.ConfirmConsentAsync();
        }

        public Task<RawbbitRecoveryResult> WithdrawConsentAsync()
        {
            // Update() observes the synchronous consent gate and aborts an
            // active request as best effort. The Collector may already accept it.
            return Client == null
                ? Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_not_ready" })
                : Client.WithdrawConsentAsync();
        }

        public Task<RawbbitTrackResult> TrackAsync(RawbbitEventInput input)
        {
            return Client == null
                ? Task.FromResult(new RawbbitTrackResult { Status = RawbbitTrackStatus.ConsentUnknown, SafeDiagnosticCode = "client_not_ready" })
                : Client.TrackAsync(input);
        }

        public Task<RawbbitRecoveryResult> RetryQuarantinedAsync(
            IList<string> opaqueHandles,
            long expectedFaultRevision,
            long expectedQueueGeneration,
            string reasonCode,
            string policyVersion)
        {
            return Client == null
                ? Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_not_ready" })
                : Client.RetryQuarantinedAsync(opaqueHandles, expectedFaultRevision, expectedQueueGeneration, reasonCode, policyVersion);
        }

        public Task<RawbbitRecoveryResult> DiscardQuarantinedAsync(
            IList<string> opaqueHandles,
            long expectedFaultRevision,
            long expectedQueueGeneration,
            string reasonCode,
            string policyVersion)
        {
            return Client == null
                ? Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_not_ready" })
                : Client.DiscardQuarantinedAsync(opaqueHandles, expectedFaultRevision, expectedQueueGeneration, reasonCode, policyVersion);
        }

        public Task<RawbbitRecoveryResult> ValidateAndResumeAsync(
            long expectedFaultRevision,
            long expectedQueueGeneration,
            string reasonCode,
            string policyVersion)
        {
            if (Client == null) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "client_not_ready" });
            if (_destroyed) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "host_destroyed" });
            if (_recoveryRunning || _sending) return Task.FromResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "sender_busy" });

            _recoveryRunning = true;
            _activeRecoveryCompletion = new TaskCompletionSource<RawbbitRecoveryResult>(TaskCreationOptions.RunContinuationsAsynchronously);
            StartCoroutine(ValidateAndResumeRoutine(expectedFaultRevision, expectedQueueGeneration, reasonCode, policyVersion, _activeRecoveryCompletion));
            return _activeRecoveryCompletion.Task;
        }

        private IEnumerator ValidateAndResumeRoutine(
            long expectedFaultRevision,
            long expectedQueueGeneration,
            string reasonCode,
            string policyVersion,
            TaskCompletionSource<RawbbitRecoveryResult> completion)
        {
            RawbbitQueueDiagnostics diagnostics = Client.GetDiagnostics();
            long validationFaultRevision = expectedFaultRevision;
            long validationQueueGeneration = expectedQueueGeneration;
            if (diagnostics.RecoveryProbeRequired && !diagnostics.RecoveryProbeValidated)
            {
                Task<RawbbitRecoveryProbePlan> prepare = Client.PrepareRecoveryProbeAsync(expectedFaultRevision, expectedQueueGeneration);
                while (!prepare.IsCompleted) yield return null;
                if (prepare.IsFaulted || prepare.IsCanceled || !prepare.Result.Succeeded || prepare.Result.Batch == null)
                {
                    completion.TrySetResult(prepare.IsFaulted || prepare.IsCanceled
                        ? new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_probe_preparation_failed" }
                        : new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = prepare.Result.SafeDiagnosticCode });
                    _recoveryRunning = false;
                    yield break;
                }

                RawbbitPreparedBatch probeBatch = prepare.Result.Batch;
                Task<RawbbitTransportResult> send;
                if (!Client.TryStartDispatch(probeBatch, () => _transport.SendAsync(probeBatch), out send))
                {
                    completion.TrySetResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = Client.HasCurrentConsent ? "recovery_probe_start_failed" : "fresh_consent_required" });
                    _recoveryRunning = false;
                    yield break;
                }
                _sending = true;
                while (!send.IsCompleted) yield return null;
                RawbbitTransportResult response = send.IsFaulted || send.IsCanceled
                    ? new RawbbitTransportResult { StatusCode = 0, NetworkError = true }
                    : send.Result;
                Task<RawbbitRecoveryResult> finishProbe = Client.CompleteRecoveryProbeAsync(probeBatch, response);
                while (!finishProbe.IsCompleted) yield return null;
                _sending = false;
                if (finishProbe.IsFaulted || finishProbe.IsCanceled || !finishProbe.Result.Succeeded)
                {
                    completion.TrySetResult(finishProbe.IsFaulted || finishProbe.IsCanceled
                        ? new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_probe_completion_failed" }
                        : finishProbe.Result);
                    _recoveryRunning = false;
                    yield break;
                }
                diagnostics = Client.GetDiagnostics();
                validationFaultRevision = diagnostics.FaultRevision;
                validationQueueGeneration = diagnostics.QueueGeneration;
            }

            Task<RawbbitRecoveryResult> validate = Client.ValidateAndResumeAsync(
                validationFaultRevision,
                validationQueueGeneration,
                reasonCode,
                policyVersion);
            while (!validate.IsCompleted) yield return null;
            completion.TrySetResult(validate.IsFaulted || validate.IsCanceled
                ? new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "recovery_validation_failed" }
                : validate.Result);
            _recoveryRunning = false;
        }

        private void Update()
        {
            if (_destroyed || Client == null) return;
            Client.PumpMaintenance();
            if (!Client.HasCurrentConsent)
            {
                if (_sending) _transport.Abort();
                if (_prepareTask != null && _prepareTask.IsCompleted)
                {
                    if (_prepareTask.IsFaulted) { var ignored = _prepareTask.Exception; }
                    _prepareTask = null;
                }
                return;
            }
            if (_recoveryRunning) return;
            if (_sending) return;

            if (_prepareTask == null)
            {
                if (Time.realtimeSinceStartup < _nextPrepareRealtime) return;
                _prepareTask = Task.Run(() => Client.TryBeginRequest());
                return;
            }
            if (!_prepareTask.IsCompleted) return;
            Task<RawbbitPreparedBatch> completedPrepare = _prepareTask;
            _prepareTask = null;
            if (completedPrepare.IsFaulted || completedPrepare.IsCanceled)
            {
                if (completedPrepare.IsFaulted) { var ignored = completedPrepare.Exception; }
                _nextPrepareRealtime = Time.realtimeSinceStartup + 1f;
                return;
            }
            RawbbitPreparedBatch batch = completedPrepare.Result;
            if (batch == null)
            {
                _nextPrepareRealtime = Time.realtimeSinceStartup + 0.25f;
                return;
            }
            _nextPrepareRealtime = 0f;
            if (batch.RequiresLocalQuarantine)
            {
                _sending = true;
                StartCoroutine(HandleLocalOversize(batch));
            }
            else
            {
                _sending = true;
                StartCoroutine(Send(batch));
            }
        }

        private IEnumerator CompleteInitialization()
        {
            while (_openTask != null && !_openTask.IsCompleted) yield return null;
            if (_destroyed)
            {
                Task drain = _openTask != null && _openTask.Status == TaskStatus.RanToCompletion
                    ? _openTask.Result.DisposeClient()
                    : Task.CompletedTask;
                ReleaseHostWhenComplete(this, drain);
                yield break;
            }
            if (_openTask == null || _openTask.IsFaulted || _openTask.IsCanceled)
            {
                InitializationFailed = true;
                if (System.Object.ReferenceEquals(_instance, this)) _instance = null;
                yield break;
            }
            Client = _openTask.Result;
        }

        private IEnumerator HandleLocalOversize(RawbbitPreparedBatch batch)
        {
            Task completion = Client.CompleteRequestAsync(batch, new RawbbitTransportResult { StatusCode = 413, NetworkError = false });
            while (!completion.IsCompleted) yield return null;
            _sending = false;
        }

        private IEnumerator Send(RawbbitPreparedBatch batch)
        {
            Task<RawbbitTransportResult> requestTask;
            if (!Client.TryStartDispatch(batch, () => _transport.SendAsync(batch), out requestTask))
            {
                // Busy/stale/gated preparation is not a network attempt. Drop
                // this snapshot and reprepare from the current queue later.
                _nextPrepareRealtime = Time.realtimeSinceStartup + 0.25f;
                _sending = false;
                yield break;
            }
            while (!requestTask.IsCompleted) yield return null;
            RawbbitTransportResult response = requestTask.IsFaulted || requestTask.IsCanceled
                ? new RawbbitTransportResult { NetworkError = true, StatusCode = 0 }
                : requestTask.Result;
            Task completed = Client.CompleteRequestAsync(batch, response);
            while (!completed.IsCompleted) yield return null;
            _sending = false;
        }

        private void OnDestroy()
        {
            _destroyed = true;
            if (_sending) _transport.Abort();
            if (_activeRecoveryCompletion != null)
                _activeRecoveryCompletion.TrySetResult(new RawbbitRecoveryResult { Succeeded = false, SafeDiagnosticCode = "host_destroyed" });
            if (Client != null) ReleaseHostWhenComplete(this, Client.DisposeClient());
            else if (_openTask == null || _openTask.IsCompleted)
            {
                Task drain = _openTask != null && _openTask.Status == TaskStatus.RanToCompletion
                    ? _openTask.Result.DisposeClient()
                    : Task.CompletedTask;
                ReleaseHostWhenComplete(this, drain);
            }
        }

        private static void ReleaseHostWhenComplete(RawbbitUnityHost host, Task pending)
        {
            if (pending == null || pending.IsCompleted)
            {
                if (System.Object.ReferenceEquals(_instance, host)) _instance = null;
                return;
            }
            pending.ContinueWith(_ =>
            {
                if (System.Object.ReferenceEquals(_instance, host)) _instance = null;
            }, TaskScheduler.Default);
        }

        private static string ResolvePlatform(RuntimePlatform platform)
        {
            if (platform == RuntimePlatform.Android) return "android";
            if (platform == RuntimePlatform.IPhonePlayer) return "ios";
            if (platform == RuntimePlatform.WindowsEditor || platform == RuntimePlatform.OSXEditor || platform == RuntimePlatform.LinuxEditor) return "unity_editor";
            return "unity_other";
        }

        private static void AddDeviceDefault(IDictionary<string, object> device, string key, string value)
        {
            if (device != null && !String.IsNullOrWhiteSpace(value) && !device.ContainsKey(key))
                device[key] = value;
        }

        private static RawbbitConfiguration CopyConfiguration(RawbbitConfiguration source)
        {
            var copy = new RawbbitConfiguration
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
                DefaultDevice = source.DefaultDevice == null ? new System.Collections.Generic.Dictionary<string, object>() : RawbbitPayload.CloneMap(source.DefaultDevice),
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
            return copy;
        }
    }
}
