using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NUnit.Framework;

namespace Rawbbit.Unity.Tests
{
    public sealed class RawbbitSdkTests
    {
        private string _root;
        private RawbbitConfiguration _configuration;
        private FakeClock _clock;

        [SetUp]
        public void SetUp()
        {
            _root = Path.Combine(Path.GetTempPath(), "rawbbit-unity-tests", Guid.NewGuid().ToString("N"));
            _configuration = new RawbbitConfiguration
            {
                CollectorUrl = "https://collector.example.test",
                ApiKey = "test-only-not-a-real-key",
                AppId = "test.game",
                Environment = "test",
                Product = "test_product",
                Surface = "unity_game",
                Platform = "android",
                JournalCompactionBytes = 64 * 1024
            };
            _clock = new FakeClock { UtcNow = new DateTimeOffset(2026, 1, 1, 12, 0, 0, TimeSpan.Zero) };
        }

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(_root)) Directory.Delete(_root, true);
        }

        [Test]
        public void SessionTimeoutUsesStrictGreaterThanThirtyMinutes()
        {
            DateTimeOffset now = _clock.UtcNow;
            var previous = new RawbbitSessionState
            {
                SessionId = Guid.NewGuid().ToString("D"),
                LastActivityUtcTicks = now.UtcDateTime.Ticks,
                EventCounter = 4
            };

            SessionDecision exact = RawbbitIdentitySession.Decide(previous, now.AddMinutes(30), TimeSpan.FromMinutes(30), false);
            SessionDecision over = RawbbitIdentitySession.Decide(previous, now.AddMinutes(30).AddTicks(1), TimeSpan.FromMinutes(30), false);

            Assert.That(exact.IsNewSession, Is.False);
            Assert.That(exact.State.SessionId, Is.EqualTo(previous.SessionId));
            Assert.That(exact.TriggerSequence, Is.EqualTo(5));
            Assert.That(over.IsNewSession, Is.True);
            Assert.That(over.State.SessionId, Is.Not.EqualTo(previous.SessionId));
            Assert.That(over.TriggerSequence, Is.EqualTo(1));
        }

        [TestCase("0", 0)]
        [TestCase("3601", 3600)]
        [TestCase("2147483648", 3600)]
        [TestCase("999999999999999999999999999999999999", 3600)]
        public void RetryAfterDeltaSecondsAreClampedWithoutIntegerOverflow(string value, int expected)
        {
            Assert.That(RawbbitTransport.ParseRetryAfter(value), Is.EqualTo(expected));
        }

        [Test]
        public void PayloadRetainsNestedJsonAndCollectorEnvelope()
        {
            var input = new RawbbitEventInput
            {
                EventName = "match_completed",
                EventParams = new Dictionary<string, object> { ["nested"] = new Dictionary<string, object> { ["score"] = 4 } },
                UserProperties = new Dictionary<string, object> { ["inventory"] = new object[] { new Dictionary<string, object> { ["id"] = "item_a", ["quantity"] = 2 } } }
            };
            RawbbitPayload.ValidateInput(input);
            string json = RawbbitPayload.BuildEventJson(input, _configuration, "pseudo", Guid.NewGuid().ToString("D"), Guid.NewGuid().ToString("D"), _clock.UtcNow);
            JObject value = JObject.Parse(json);

            Assert.That(value["event_id"], Is.Not.Null);
            Assert.That(value["app_id"].Value<string>(), Is.EqualTo("test.game"));
            Assert.That(value["device"]["platform"].Value<string>(), Is.EqualTo("android"));
            Assert.That(value["event_params"]["nested"]["score"].Value<int>(), Is.EqualTo(4));
            Assert.That(value["user_properties"]["inventory"].First["quantity"].Value<int>(), Is.EqualTo(2));
            Assert.That(value["consent"]["analytics_storage"].Value<bool>(), Is.True);
            Assert.That(value["ingest"], Is.Null);
        }

        [Test]
        public void PayloadPreservesExplicitNullDefaultsAndOwnsPlatform()
        {
            RawbbitEventInput input = Event("neutral_event");
            input.EventParams["product"] = null;
            input.Device = new Dictionary<string, object> { ["platform"] = "spoofed-platform", ["app_version"] = "1.2.3" };
            string json = RawbbitPayload.BuildEventJson(input, _configuration, "pseudo", Guid.NewGuid().ToString("D"), Guid.NewGuid().ToString("D"), _clock.UtcNow);
            JObject value = JObject.Parse(json);

            Assert.That(value["event_params"]["product"].Type, Is.EqualTo(Newtonsoft.Json.Linq.JTokenType.Null));
            Assert.That(value["device"]["platform"].Value<string>(), Is.EqualTo("android"));
            Assert.That(value["device"]["app_version"].Value<string>(), Is.EqualTo("1.2.3"));
        }

        [Test]
        public void PayloadRejectsCyclesDatesAndNonFiniteNumbers()
        {
            var cycle = new Dictionary<string, object>();
            cycle["self"] = cycle;
            RawbbitEventInput cyclical = Event("cycle");
            cyclical.EventParams["cycle"] = cycle;
            Assert.Throws<ArgumentException>(() => RawbbitPayload.ValidateInput(cyclical));

            RawbbitEventInput date = Event("date");
            date.EventParams["unsupported"] = DateTime.UtcNow;
            Assert.Throws<ArgumentException>(() => RawbbitPayload.ValidateInput(date));

            RawbbitEventInput notFinite = Event("non_finite");
            notFinite.EventParams["unsupported"] = Double.NaN;
            Assert.Throws<ArgumentException>(() => RawbbitPayload.ValidateInput(notFinite));
        }

        [Test]
        public async Task TrackingFreezesInputBeforeReturningAndRequiresConsent()
        {
            RawbbitClient client = await Open();
            RawbbitTrackResult beforeConsent = await client.TrackAsync(Event("before_consent"));
            Assert.That(beforeConsent.Status, Is.EqualTo(RawbbitTrackStatus.ConsentUnknown));
            Assert.That((await client.ConfirmConsentAsync()).Succeeded, Is.True);

            var nested = new Dictionary<string, object> { ["score"] = 10 };
            RawbbitEventInput input = Event("frozen_event");
            input.EventParams = new Dictionary<string, object> { ["nested"] = nested };
            Task<RawbbitTrackResult> tracking = client.TrackAsync(input);
            nested["score"] = 999;
            Assert.That((await tracking).Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));

            RawbbitPreparedBatch batch = client.TryBeginRequest(_clock.UtcNow);
            Assert.That(batch.Body, Does.Contain("\"score\":10"));
            Assert.That(batch.Body, Does.Not.Contain("999"));
            client.Dispose();
        }

        [Test]
        public async Task InvalidConfigurationDoesNotPruneExistingQueue()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("preserve_on_invalid_config"));
            client.Dispose();

            RawbbitConfiguration invalid = new RawbbitConfiguration
            {
                CollectorUrl = _configuration.CollectorUrl,
                ApiKey = _configuration.ApiKey,
                AppId = _configuration.AppId,
                Environment = _configuration.Environment,
                Platform = _configuration.Platform,
                RetentionDays = -1
            };
            Assert.Throws<ArgumentOutOfRangeException>(() => RawbbitClient.OpenAsync(invalid, _root, _clock));

            RawbbitClient recovered = await Open();
            Assert.That(recovered.GetDiagnostics().PendingCount, Is.EqualTo(1));
            recovered.Dispose();
        }

        [Test]
        public async Task TrackingTimeAndSessionOrderingAreCapturedBeforeWorkerProcessing()
        {
            RawbbitEventContextMapper mapper = (context, triggering) =>
            {
                if (triggering.EventParams == null) triggering.EventParams = new Dictionary<string, object>();
                triggering.EventParams["sequence"] = context.TriggeringEventSequence;
            };
            RawbbitClient client = await RawbbitClient.OpenAsync(_configuration, _root, _clock, null, null, null, mapper);
            await client.ConfirmConsentAsync();

            string expectedFirstTimestamp = RawbbitPayload.FormatTimestampForSdk(null, _clock.UtcNow);
            Task<RawbbitTrackResult> first = client.TrackAsync(Event("first_ordered"));
            _clock.UtcNow = _clock.UtcNow.AddMinutes(40);
            Task<RawbbitTrackResult> second = client.TrackAsync(Event("second_ordered"));
            Assert.That((await first).Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));
            Assert.That((await second).Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));

            JObject body = JObject.Parse(client.TryBeginRequest(_clock.UtcNow).Body);
            Assert.That(body["events"][0]["event_name"].Value<string>(), Is.EqualTo("first_ordered"));
            Assert.That(body["events"][0]["event_timestamp"].Value<string>(), Is.EqualTo(expectedFirstTimestamp));
            Assert.That(body["events"][1]["event_name"].Value<string>(), Is.EqualTo("second_ordered"));
            Assert.That(body["events"][0]["user"]["session_id"].Value<string>(), Is.Not.EqualTo(body["events"][1]["user"]["session_id"].Value<string>()));
            Assert.That(body["events"][0]["event_params"]["sequence"].Value<int>(), Is.EqualTo(1));
            Assert.That(body["events"][1]["event_params"]["sequence"].Value<int>(), Is.EqualTo(1));
            client.Dispose();
        }

        [Test]
        public async Task RestartRequiresFreshConsentBeforeReplay()
        {
            RawbbitClient first = await Open();
            await first.ConfirmConsentAsync();
            await first.TrackAsync(Event("queued_before_restart"));
            first.Dispose();

            RawbbitClient restarted = await Open();
            Assert.That(restarted.TryBeginRequest(_clock.UtcNow), Is.Null);
            Assert.That((await restarted.TrackAsync(Event("not_allowed"))).Status, Is.EqualTo(RawbbitTrackStatus.ConsentUnknown));
            Assert.That(restarted.GetDiagnostics().PendingCount, Is.EqualTo(1));

            Assert.That((await restarted.ConfirmConsentAsync()).Succeeded, Is.True);
            Assert.That(restarted.TryBeginRequest(_clock.UtcNow), Is.Not.Null);
            restarted.Dispose();
        }

        [Test]
        public async Task WithdrawalPurgesQueueResetsSessionAndRetainsInstallationIdentity()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("before_withdrawal"));
            string originalPseudoId = ReadUserField(client.TryBeginRequest(_clock.UtcNow), "user_pseudo_id");
            string originalSessionId = ReadUserField(client.TryBeginRequest(_clock.UtcNow), "session_id");

            RawbbitRecoveryResult withdrawal = await client.WithdrawConsentAsync();
            Assert.That(withdrawal.Succeeded, Is.True);
            Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            string storeRoot = Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Single();
            foreach (string file in Directory.GetFiles(storeRoot, "*", SearchOption.AllDirectories))
                Assert.That(Encoding.UTF8.GetString(File.ReadAllBytes(file)), Does.Not.Contain("before_withdrawal"), file);
            Assert.That((await client.ConfirmConsentAsync()).Succeeded, Is.True);
            await client.TrackAsync(Event("after_reconsent"));
            RawbbitPreparedBatch after = client.TryBeginRequest(_clock.UtcNow);

            Assert.That(ReadUserField(after, "user_pseudo_id"), Is.EqualTo(originalPseudoId));
            Assert.That(ReadUserField(after, "session_id"), Is.Not.EqualTo(originalSessionId));
            client.Dispose();
        }

        [Test]
        public async Task WithdrawalInvalidatesPreparedDispatchBeforeTransportStart()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("not_dispatched_after_withdrawal"));
            RawbbitPreparedBatch prepared = client.TryBeginRequest(_clock.UtcNow);
            int starts = 0;
            Task<RawbbitRecoveryResult> withdrawal = client.WithdrawConsentAsync();

            Task<RawbbitTransportResult> request;
            bool dispatched = client.TryStartDispatch(prepared, () =>
            {
                starts++;
                return Task.FromResult(new RawbbitTransportResult { StatusCode = 200 });
            }, out request);

            Assert.That(dispatched, Is.False);
            Assert.That(starts, Is.Zero);
            Assert.That((await withdrawal).Succeeded, Is.True);
            Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            client.Dispose();
        }

        [Test]
        public async Task TrackCallWaitingOnIdentityProviderCannotCrossWithdrawalAndReconsent()
        {
            var provider = new BlockingIdentityProvider();
            RawbbitClient client = await RawbbitClient.OpenAsync(_configuration, _root, _clock, provider);
            await client.ConfirmConsentAsync();

            Task<RawbbitTrackResult> tracking = Task.Run(async () => await client.TrackAsync(Event("pre_withdrawal_work")));
            Assert.That(await Task.Run(() => provider.Started.Wait(TimeSpan.FromSeconds(5))), Is.True);
            Task<RawbbitRecoveryResult> withdrawal = client.WithdrawConsentAsync();
            provider.Continue.Set();

            Assert.That((await withdrawal).Succeeded, Is.True);
            Assert.That((await tracking).Status, Is.Not.EqualTo(RawbbitTrackStatus.DurablyStored));
            Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            Assert.That((await client.ConfirmConsentAsync()).Succeeded, Is.True);
            Assert.That((await client.TrackAsync(Event("post_reconsent_work"))).Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));
            client.Dispose();
            provider.Dispose();
        }

        [Test]
        public async Task SessionStartFactoryPrefixAndTriggerAreOrderedAndCounted()
        {
            RawbbitSessionStartFactory factory = (context, triggering) => new RawbbitEventInput
            {
                EventName = "session_started",
                EventParams = new Dictionary<string, object> { ["sequence"] = context.SessionStartedEventSequence },
                UserProperties = triggering.UserProperties
            };
            RawbbitEventContextMapper mapper = (context, triggering) =>
            {
                if (triggering.EventParams == null) triggering.EventParams = new Dictionary<string, object>();
                triggering.EventParams["sequence"] = context.TriggeringEventSequence;
            };
            RawbbitClient client = await RawbbitClient.OpenAsync(_configuration, _root, _clock, null, factory, null, mapper);
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("neutral_action"));
            await client.TrackAsync(Event("second_action"));
            RawbbitPreparedBatch batch = client.TryBeginRequest(_clock.UtcNow);
            JObject body = JObject.Parse(batch.Body);
            JArray events = (JArray)body["events"];

            Assert.That(events.Count, Is.EqualTo(3));
            Assert.That(events[0]["event_name"].Value<string>(), Is.EqualTo("session_started"));
            Assert.That(events[0]["event_params"]["sequence"].Value<int>(), Is.EqualTo(1));
            Assert.That(events[1]["event_name"].Value<string>(), Is.EqualTo("neutral_action"));
            Assert.That(events[1]["event_params"]["sequence"].Value<int>(), Is.EqualTo(2));
            Assert.That(events[2]["event_name"].Value<string>(), Is.EqualTo("second_action"));
            Assert.That(events[2]["event_params"]["sequence"].Value<int>(), Is.EqualTo(3));
            Assert.That(events[0]["user"]["session_id"].Value<string>(), Is.EqualTo(events[1]["user"]["session_id"].Value<string>()));
            client.Dispose();
        }

        [Test]
        public async Task OptionalSessionPrefixMayBeOmittedAndTriggerStartsAtSequenceOne()
        {
            RawbbitSessionStartFactory factory = (context, triggering) => null;
            RawbbitEventContextMapper mapper = (context, triggering) =>
            {
                if (triggering.EventParams == null) triggering.EventParams = new Dictionary<string, object>();
                triggering.EventParams["sequence"] = context.TriggeringEventSequence;
            };
            RawbbitClient client = await RawbbitClient.OpenAsync(_configuration, _root, _clock, null, factory, null, mapper);
            await client.ConfirmConsentAsync();

            RawbbitTrackResult tracked = await client.TrackAsync(Event("without_prefix"));
            RawbbitPreparedBatch batch = client.TryBeginRequest(_clock.UtcNow);
            JObject body = JObject.Parse(batch.Body);

            Assert.That(tracked.Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));
            Assert.That(body["events"].Count(), Is.EqualTo(1));
            Assert.That(body["events"][0]["event_params"]["sequence"].Value<int>(), Is.EqualTo(1));
            client.Dispose();
        }

        [Test]
        public async Task Http422QuarantinesAndExplicitRecoveryDisposesOnlySelectedRecord()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("malformed_frozen_event"));
            RawbbitPreparedBatch rejected = client.TryBeginRequest(_clock.UtcNow);
            await client.CompleteRequestAsync(rejected, new RawbbitTransportResult { StatusCode = 422, NetworkError = false });
            Assert.That(client.GetDiagnostics().QuarantinedCount, Is.EqualTo(1));
            Assert.That(client.TryBeginRequest(_clock.UtcNow), Is.Null);

            await client.TrackAsync(Event("later_valid_event"));
            RawbbitQueueDiagnostics before = client.GetDiagnostics();
            RawbbitQuarantinedRecordInfo quarantined = client.ListQuarantinedRecords().Single();
            RawbbitRecoveryResult discarded = await client.DiscardQuarantinedAsync(
                new[] { quarantined.Handle }, before.FaultRevision, before.QueueGeneration, "invalid_frozen_event", "test_policy_v1");
            Assert.That(discarded.Succeeded, Is.True);
            await client.TrackAsync(Event("later_valid_after_probe"));
            RawbbitQueueDiagnostics afterDispose = client.GetDiagnostics();
            Assert.That(afterDispose.ExplicitlyDiscardedCount, Is.EqualTo(1));
            Assert.That(afterDispose.FaultCode, Is.Not.Null);

            RawbbitRecoveryProbePlan probe = await client.PrepareRecoveryProbeAsync(afterDispose.FaultRevision, afterDispose.QueueGeneration);
            Assert.That(probe.Succeeded, Is.True);
            Assert.That(probe.Batch.Body, Does.Contain("later_valid_event"));
            RawbbitRecoveryResult probed = await client.CompleteRecoveryProbeAsync(probe.Batch, new RawbbitTransportResult
            {
                StatusCode = 200,
                NetworkError = false,
                ResponseBody = "{\"accepted_events\":1}"
            });
            Assert.That(probed.Succeeded, Is.True);
            RawbbitQueueDiagnostics afterProbe = client.GetDiagnostics();
            Assert.That(afterProbe.RecoveryProbeValidated, Is.True);
            RawbbitRecoveryResult resumed = await client.ValidateAndResumeAsync(
                afterProbe.FaultRevision, afterProbe.QueueGeneration, "sdk_fix_validated", "test_policy_v1");
            Assert.That(resumed.Succeeded, Is.True);
            RawbbitPreparedBatch valid = client.TryBeginRequest(_clock.UtcNow);
            Assert.That(valid.Body, Does.Contain("later_valid_after_probe"));
            Assert.That(valid.Body, Does.Not.Contain("later_valid_event"));
            Assert.That(valid.Body, Does.Not.Contain("malformed_frozen_event"));
            client.Dispose();
        }

        [Test]
        public async Task ExplicitRetryPreservesFrozenEventBytesBeforeValidatedResume()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("original_frozen_event"));
            RawbbitPreparedBatch original = client.TryBeginRequest(_clock.UtcNow);
            await client.CompleteRequestAsync(original, new RawbbitTransportResult { StatusCode = 422, NetworkError = false });
            RawbbitQueueDiagnostics faulted = client.GetDiagnostics();
            RawbbitQuarantinedRecordInfo record = client.ListQuarantinedRecords().Single();

            RawbbitRecoveryResult retried = await client.RetryQuarantinedAsync(
                new[] { record.Handle }, faulted.FaultRevision, faulted.QueueGeneration,
                "configuration_corrected", "test_policy_v1");
            Assert.That(retried.Succeeded, Is.True);
            RawbbitQueueDiagnostics retryState = client.GetDiagnostics();
            Assert.That(retryState.FaultCode, Is.Not.Null);
            Assert.That(retryState.QuarantinedCount, Is.Zero);

            RawbbitRecoveryProbePlan probe = await client.PrepareRecoveryProbeAsync(retryState.FaultRevision, retryState.QueueGeneration);
            Assert.That(probe.Succeeded, Is.True);
            Assert.That(probe.Batch.Body, Is.EqualTo(original.Body));
            Assert.That((await client.CompleteRecoveryProbeAsync(probe.Batch, new RawbbitTransportResult
            {
                StatusCode = 200,
                NetworkError = false,
                ResponseBody = "{\"accepted_events\":1}"
            })).Succeeded, Is.True);
            RawbbitQueueDiagnostics afterProbe = client.GetDiagnostics();
            RawbbitRecoveryResult resumed = await client.ValidateAndResumeAsync(
                afterProbe.FaultRevision, afterProbe.QueueGeneration, "configuration_corrected", "test_policy_v1");
            Assert.That(resumed.Succeeded, Is.True);
            Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            client.Dispose();
        }

        [Test]
        public void RecoveryValidationChunksLargeInvalidPendingSetAfterProbe()
        {
            _configuration.MaxQueueBytes = 10L * 1024L * 1024L;
            _configuration.JournalCompactionBytes = 8L * 1024L * 1024L;
            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            var emptySession = new RawbbitSessionState();
            string sessionId = Guid.NewGuid().ToString("D");
            string validId = Guid.NewGuid().ToString("D");
            string validPayload = RawbbitPayload.BuildEventJson(
                Event("recovery_probe_event"), _configuration, queue.InstallationId, sessionId, validId, _clock.UtcNow);
            queue.Enqueue(new[]
            {
                new RawbbitQueueRecord { Handle = Guid.NewGuid().ToString("N"), EventId = validId, PayloadJson = validPayload, EnqueuedUtcTicks = _clock.UtcNow.UtcDateTime.Ticks }
            }, emptySession, _clock.UtcNow);

            for (int index = 0; index < 8; index++)
            {
                queue.Enqueue(new[]
                {
                    new RawbbitQueueRecord
                    {
                        Handle = Guid.NewGuid().ToString("N"),
                        EventId = Guid.NewGuid().ToString("D"),
                        PayloadJson = "invalid-json-" + new string('x', 900_000),
                        EnqueuedUtcTicks = _clock.UtcNow.UtcDateTime.Ticks
                    }
                }, emptySession, _clock.UtcNow);
            }

            queue.LatchFault("review_bulk_validation", 422, _clock.UtcNow, true);
            RawbbitQueueDiagnostics faulted = queue.ReadDiagnostics();
            RawbbitQueueRecord probe = queue.GetRecoveryProbeRecord(_clock.UtcNow);
            Assert.That(probe.EventId, Is.EqualTo(validId));
            queue.MarkRecoveryProbeValidated(faulted.FaultRevision, faulted.QueueGeneration, probe.Handle, _clock.UtcNow);

            RawbbitQueueDiagnostics afterProbe = queue.ReadDiagnostics();
            RawbbitRecoveryResult validation = queue.ValidateAndResume(
                afterProbe.FaultRevision, afterProbe.QueueGeneration, "sdk_fix_validated", "bulk_validation_test", _clock.UtcNow);
            RawbbitQueueDiagnostics afterValidation = queue.ReadDiagnostics();

            Assert.That(validation.Succeeded, Is.False);
            Assert.That(validation.SafeDiagnosticCode, Is.EqualTo("invalid_records_quarantined_for_review"));
            Assert.That(afterValidation.PendingCount, Is.Zero);
            Assert.That(afterValidation.QuarantinedCount, Is.EqualTo(8));
            Assert.That(afterValidation.FaultCode, Is.EqualTo("recovery_validation_requires_review"));
            Assert.That(queue.ListQuarantined().Count, Is.EqualTo(8));
        }

        [Test]
        public async Task CapacityEvictionReportsQuarantinedRecordLossSeparately()
        {
            _configuration.MaxQueueBytes = 2048;
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("rejected_record"));
            RawbbitPreparedBatch rejected = client.TryBeginRequest(_clock.UtcNow);
            await client.CompleteRequestAsync(rejected, new RawbbitTransportResult { StatusCode = 422, NetworkError = false });

            for (int i = 0; i < 3; i++) await client.TrackAsync(Event("later_" + i));
            RawbbitQueueDiagnostics diagnostics = client.GetDiagnostics();

            Assert.That(diagnostics.FaultCode, Is.Not.Null);
            Assert.That(diagnostics.EvictedCount, Is.GreaterThanOrEqualTo(1));
            Assert.That(diagnostics.QuarantinedEvictedCount, Is.EqualTo(1));
            Assert.That(diagnostics.PendingCount + diagnostics.QuarantinedCount, Is.LessThanOrEqualTo(2));
            client.Dispose();
        }

        [Test]
        public async Task QueueEvictsOldestEventWithinConfiguredLogicalLimit()
        {
            _configuration.MaxQueueBytes = 2048;
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            for (int i = 0; i < 4; i++)
            {
                RawbbitEventInput input = Event("event_" + i);
                input.EventParams = new Dictionary<string, object> { ["padding"] = new string((char)('a' + i), 300) };
                RawbbitTrackResult result = await client.TrackAsync(input);
                Assert.That(result.Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));
            }
            RawbbitQueueDiagnostics diagnostics = client.GetDiagnostics();
            Assert.That(diagnostics.QueueBytes, Is.LessThanOrEqualTo(2048));
            Assert.That(diagnostics.EvictedCount, Is.GreaterThan(0));
            RawbbitPreparedBatch batch = client.TryBeginRequest(_clock.UtcNow);
            Assert.That(batch.Body, Does.Not.Contain("event_0"));
            client.Dispose();
        }

        [Test]
        public async Task SessionPrefixAndTriggerAreRejectedTogetherWhenCombinedSizeExceedsQueueLimit()
        {
            _configuration.MaxQueueBytes = 2048;
            RawbbitSessionStartFactory factory = (context, triggering) => new RawbbitEventInput
            {
                EventName = "generic_session_prefix",
                EventParams = new Dictionary<string, object> { ["padding"] = new string('s', 700) },
                UserProperties = new Dictionary<string, object>()
            };
            RawbbitClient client = await RawbbitClient.OpenAsync(_configuration, _root, _clock, null, factory);
            await client.ConfirmConsentAsync();

            RawbbitTrackResult result = await client.TrackAsync(Event("generic_trigger"));

            Assert.That(result.Status, Is.EqualTo(RawbbitTrackStatus.InvalidEvent));
            Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            Assert.That(client.GetDiagnostics().QueueBytes, Is.Zero);
            client.Dispose();
        }

        [Test]
        public async Task RetentionExpiresAtSevenDaysFromEnqueueBoundary()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("expires_at_seven_days"));

            _clock.UtcNow = _clock.UtcNow.AddDays(7).AddTicks(-1);
            Assert.That(client.TryBeginRequest(_clock.UtcNow), Is.Not.Null);
            Assert.That(client.GetDiagnostics().PendingCount, Is.EqualTo(1));

            _clock.UtcNow = _clock.UtcNow.AddTicks(1);
            Assert.That(client.TryBeginRequest(_clock.UtcNow), Is.Null);
            Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            Assert.That(client.GetDiagnostics().ExpiredCount, Is.EqualTo(1));
            client.Dispose();
        }

        [Test]
        public async Task PartialJournalTailIsIgnoredAfterProcessRestart()
        {
            RawbbitClient client = await Open();
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("journal_recovery"));
            client.Dispose();
            string journal = Directory.GetFiles(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Single(), "journal-*.bin").OrderByDescending(File.GetLastWriteTimeUtc).First();
            using (var stream = new FileStream(journal, FileMode.Append, FileAccess.Write, FileShare.None))
            {
                stream.WriteByte(0x01);
                stream.WriteByte(0x02);
            }

            RawbbitClient recovered = await Open();
            Assert.That(recovered.GetDiagnostics().PendingCount, Is.EqualTo(1));
            Assert.That((await recovered.ConfirmConsentAsync()).Succeeded, Is.True);
            Assert.That(recovered.TryBeginRequest(_clock.UtcNow), Is.Not.Null);
            recovered.Dispose();
        }

        [TestCase(401)]
        [TestCase(422)]
        public async Task StableStoreEndpointChangeRequiresAuthorizationAndPreservesFrozenState(int statusCode)
        {
            string previousUrl = _configuration.CollectorUrl;
            var originalQueue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            var originalClient = new RawbbitClient(_configuration, originalQueue, _clock);
            await originalClient.ConfirmConsentAsync();
            await originalClient.TrackAsync(Event("frozen_before_endpoint_change"));
            RawbbitPreparedBatch original = originalClient.TryBeginRequest();
            await originalClient.CompleteRequestAsync(original, new RawbbitTransportResult { StatusCode = statusCode });
            string installationId = originalQueue.InstallationId;
            string sessionJson = JsonConvert.SerializeObject(originalQueue.ReadSession());
            RawbbitFaultState originalFault = originalQueue.ReadFault();
            long generation = originalQueue.ReadDiagnostics().QueueGeneration;
            string storeRoot = Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Single();
            Assert.That(Path.GetFileName(storeRoot), Does.StartWith("store-v2-"));
            await originalClient.DisposeClient();

            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = true;
            _configuration.CollectorUrl = "https://replacement.example.test";
            Assert.ThrowsAsync<InvalidDataException>(async () => { await Open(); });
            _configuration.PreviousCollectorUrl = "https://wrong-source.example.test";
            Assert.ThrowsAsync<InvalidDataException>(async () => { await Open(); });
            _configuration.PreviousCollectorUrl = previousUrl;
            RawbbitClient migrated = await Open();
            Assert.That(migrated.HasCurrentConsent, Is.False);
            Assert.That(migrated.TryBeginRequest(), Is.Null);
            Assert.That((await migrated.ConfirmConsentAsync()).Succeeded, Is.True);
            RawbbitQueueDiagnostics diagnostics = migrated.GetDiagnostics();
            Assert.That(diagnostics.PendingCount, Is.EqualTo(statusCode == 422 ? 0 : 1));
            Assert.That(diagnostics.QuarantinedCount, Is.EqualTo(statusCode == 422 ? 1 : 0));
            Assert.That(diagnostics.QueueGeneration, Is.EqualTo(generation + 1));
            Assert.That(diagnostics.FaultRevision, Is.GreaterThan(originalFault.Revision));
            Assert.That(migrated.TryBeginRequest(), Is.Null, "Migration must not clear the delivery fault.");
            if (statusCode == 422)
            {
                RawbbitQuarantinedRecordInfo quarantined = migrated.ListQuarantinedRecords().Single();
                Assert.That(quarantined.Handle, Is.EqualTo(original.Handles.Single()));
                Assert.That(quarantined.HttpStatusCode, Is.EqualTo(422));
                Assert.That(quarantined.EnqueuedAtUtc.UtcDateTime.Ticks, Is.EqualTo(original.Records.Single().EnqueuedUtcTicks));
                Assert.That((await migrated.RetryQuarantinedAsync(new[] { quarantined.Handle }, diagnostics.FaultRevision,
                    diagnostics.QueueGeneration, "configuration_corrected", "test_policy_v1")).Succeeded, Is.True);
                diagnostics = migrated.GetDiagnostics();
            }
            RawbbitRecoveryProbePlan probe = await migrated.PrepareRecoveryProbeAsync(diagnostics.FaultRevision, diagnostics.QueueGeneration);
            Assert.That(probe.Succeeded, Is.True);
            Assert.That(probe.Batch.Body, Is.EqualTo(original.Body));
            Assert.That(probe.Batch.Endpoint, Is.EqualTo("https://replacement.example.test/v1/events:batch"));
            await migrated.DisposeClient();

            _configuration.PreviousCollectorUrl = null;
            var restarted = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(restarted.InstallationId, Is.EqualTo(installationId));
            Assert.That(JsonConvert.SerializeObject(restarted.ReadSession()), Is.EqualTo(sessionJson));
            AssertFaultAfterEndpointBinding(originalFault, restarted.ReadFault(), true);
            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")), Is.EqualTo(new[] { storeRoot }));
            using (var client = new RawbbitClient(_configuration, restarted, _clock))
            {
                await client.ConfirmConsentAsync();
                Assert.That((await client.TrackAsync(Event("same_session_after_migration"))).IsDurablyStored, Is.True);
                Assert.That(restarted.ReadSession().SessionId, Is.EqualTo(ReadUserField(original, "session_id")));
                Assert.That(restarted.ReadSession().EventCounter, Is.EqualTo(2));
            }
            _configuration.CollectorUrl = previousUrl;
            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));
        }

        [TestCase(false)]
        [TestCase(true)]
        public void VersionOneLegacyStoreIsAdoptedAndEndpointBindingSurvivesRestart(bool changeEndpoint)
        {
            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = true;
            string previousUrl = _configuration.CollectorUrl;
            RawbbitPersistentState fixture = WriteLegacyStore(previousUrl, true);
            string legacyRoot = LegacyStoreRoot(previousUrl);
            File.WriteAllText(Path.Combine(legacyRoot, "rename-witness.txt"), "must move with the directory");
            if (changeEndpoint)
            {
                _configuration.CollectorUrl = "https://replacement.example.test";
                _configuration.PreviousCollectorUrl = previousUrl;
            }

            var adopted = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(Directory.Exists(legacyRoot), Is.False);
            string stableRoot = Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Single();
            Assert.That(Path.GetFileName(stableRoot), Does.StartWith("store-v2-"));
            Assert.That(File.ReadAllText(Path.Combine(stableRoot, "rename-witness.txt")), Is.EqualTo("must move with the directory"));
            Assert.That(adopted.InstallationId, Is.EqualTo(fixture.InstallationId));
            Assert.That(JsonConvert.SerializeObject(adopted.ReadSession()), Is.EqualTo(JsonConvert.SerializeObject(fixture.Session)));
            AssertFaultAfterEndpointBinding(fixture.Fault, adopted.ReadFault(), changeEndpoint);
            Assert.That(adopted.ReadDiagnostics().QueueBytes, Is.EqualTo(fixture.Records.Single().QueueBytes));
            Assert.That(adopted.ReadDiagnostics().QueueGeneration, Is.EqualTo(fixture.QueueGeneration + 1));
            Assert.That(JsonConvert.SerializeObject(adopted.GetRecoveryProbeRecord(_clock.UtcNow)), Is.EqualTo(JsonConvert.SerializeObject(fixture.Records.Single())));

            _configuration.PreviousCollectorUrl = null;
            var restarted = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(restarted.ReadDiagnostics().QueueGeneration, Is.EqualTo(adopted.ReadDiagnostics().QueueGeneration));
            Assert.That(JsonConvert.SerializeObject(restarted.ReadFault()), Is.EqualTo(JsonConvert.SerializeObject(adopted.ReadFault())));
            Assert.That(restarted.GetRecoveryProbeRecord(_clock.UtcNow).PayloadJson, Is.EqualTo(fixture.Records.Single().PayloadJson));
            _configuration.CollectorUrl = changeEndpoint ? previousUrl : "https://unauthorized.example.test";
            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));
        }

        [TestCase(false)]
        [TestCase(true)]
        public void ConflictingStoresFailWithoutMergingOrChangingFiles(bool stableStoreExists)
        {
            string previousUrl = _configuration.CollectorUrl;
            _configuration.CollectorUrl = "https://replacement.example.test";
            if (stableStoreExists)
                new RawbbitPersistentQueue(_root, _configuration, _clock);
            else
                WriteLegacyStore(_configuration.CollectorUrl, false);
            WriteLegacyStore(previousUrl, false);
            _configuration.PreviousCollectorUrl = previousUrl;
            string[] directories = Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).OrderBy(path => path).ToArray();
            var files = Directory.GetFiles(Path.Combine(_root, "Rawbbit"), "*", SearchOption.AllDirectories)
                .ToDictionary(path => path, File.ReadAllBytes);

            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = true;
            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));

            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).OrderBy(path => path), Is.EqualTo(directories));
            Assert.That(Directory.GetFiles(Path.Combine(_root, "Rawbbit"), "*", SearchOption.AllDirectories), Is.EquivalentTo(files.Keys));
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value), file.Key);
        }

        [TestCase("fresh")]
        [TestCase("other_app")]
        [TestCase("other_environment")]
        public void FreshStoreAcceptsPreviousCollectorUrlWithoutLegacyMigrationSource(string existingStore)
        {
            string previousUrl = _configuration.CollectorUrl;
            string unrelatedInstallationId = null;
            if (existingStore != "fresh")
            {
                unrelatedInstallationId = new RawbbitPersistentQueue(_root, _configuration, _clock).InstallationId;
                if (existingStore == "other_app") _configuration.AppId = "other.game";
                else _configuration.Environment = "other_environment";
            }
            _configuration.CollectorUrl = "https://replacement.example.test";
            _configuration.PreviousCollectorUrl = previousUrl;

            var fresh = new RawbbitPersistentQueue(_root, _configuration, _clock);

            Guid installationId;
            Assert.That(Guid.TryParse(fresh.InstallationId, out installationId), Is.True);
            Assert.That(fresh.InstallationId, Is.Not.EqualTo(unrelatedInstallationId));
            Assert.That(fresh.ReadDiagnostics().PendingCount, Is.Zero);
            Assert.That(fresh.ReadSession().SessionId, Is.Null);
            Assert.That(fresh.ReadFault(), Is.Null);
            string[] stores = Directory.GetDirectories(Path.Combine(_root, "Rawbbit"));
            Assert.That(stores.Length, Is.EqualTo(existingStore == "fresh" ? 1 : 2));
            Assert.That(stores.All(path => Path.GetFileName(path).StartsWith("store-v2-", StringComparison.Ordinal)), Is.True);
            _configuration.PreviousCollectorUrl = null;
            Assert.That(new RawbbitPersistentQueue(_root, _configuration, _clock).InstallationId, Is.EqualTo(fresh.InstallationId));
        }

        [TestCase(false)]
        [TestCase(true)]
        public async Task FreshNamespaceRequiresExplicitAuthorizationAndLeavesUnresolvedLegacyStoreUntouched(bool changeApp)
        {
            string appId = _configuration.AppId;
            string environment = _configuration.Environment;
            RawbbitPersistentState fixture = WriteLegacyStore(_configuration.CollectorUrl, true);
            string legacyRoot = LegacyStoreRoot(_configuration.CollectorUrl);
            var files = Directory.GetFiles(legacyRoot, "*", SearchOption.AllDirectories)
                .ToDictionary(path => path, File.ReadAllBytes);
            if (changeApp) _configuration.AppId = "other.game";
            else _configuration.Environment = "other_environment";

            Assert.That(_configuration.AllowFreshStoreWithUnresolvedLegacyStores, Is.False);
            Assert.ThrowsAsync<InvalidDataException>(async () => { await Open(); });
            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")), Is.EqualTo(new[] { legacyRoot }));
            Assert.That(Directory.GetFiles(legacyRoot, "*", SearchOption.AllDirectories), Is.EquivalentTo(files.Keys));
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value), file.Key);

            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = true;
            using (RawbbitClient fresh = await Open())
            {
                Assert.That(fresh.HasCurrentConsent, Is.False);
                Assert.That(fresh.GetDiagnostics().PendingCount, Is.Zero);
                Assert.That(fresh.GetDiagnostics().QuarantinedCount, Is.Zero);
                Assert.That(fresh.GetDiagnostics().QueueBytes, Is.Zero);
                Assert.That(fresh.GetDiagnostics().FaultCode, Is.Null);
                Assert.That(fresh.TryBeginRequest(), Is.Null);
                Assert.That((await fresh.TrackAsync(Event("fresh_without_consent"))).Status, Is.EqualTo(RawbbitTrackStatus.ConsentUnknown));
            }
            var freshQueue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            string freshInstallationId = freshQueue.InstallationId;
            Guid parsedInstallationId;
            Assert.That(Guid.TryParse(freshInstallationId, out parsedInstallationId), Is.True);
            Assert.That(freshInstallationId, Is.Not.EqualTo(fixture.InstallationId));
            Assert.That(JsonConvert.SerializeObject(freshQueue.ReadSession()), Is.EqualTo(JsonConvert.SerializeObject(new RawbbitSessionState())));
            Assert.That(freshQueue.ReadFault(), Is.Null);

            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = false;
            using (RawbbitClient restarted = await Open())
            {
                Assert.That(restarted.HasCurrentConsent, Is.False);
                Assert.That(restarted.GetDiagnostics().PendingCount, Is.Zero);
                Assert.That(restarted.GetDiagnostics().FaultCode, Is.Null);
            }
            Assert.That(new RawbbitPersistentQueue(_root, _configuration, _clock).InstallationId, Is.EqualTo(freshInstallationId));
            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Length, Is.EqualTo(2));
            Assert.That(Directory.Exists(legacyRoot), Is.True);
            Assert.That(Directory.GetFiles(legacyRoot, "*", SearchOption.AllDirectories), Is.EquivalentTo(files.Keys));
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value), file.Key);

            _configuration.AppId = appId;
            _configuration.Environment = environment;
            using (RawbbitClient restored = await Open())
            {
                Assert.That(restored.HasCurrentConsent, Is.False);
                Assert.That(restored.TryBeginRequest(), Is.Null);
                Assert.That((await restored.ConfirmConsentAsync()).Succeeded, Is.True);
                Assert.That(restored.GetDiagnostics().PendingCount, Is.EqualTo(1));
                Assert.That(restored.GetDiagnostics().FaultCode, Is.EqualTo(fixture.Fault.FaultCode));
                Assert.That(restored.TryBeginRequest(), Is.Null, "Adoption must preserve the delivery fault.");
                RawbbitQueueDiagnostics diagnostics = restored.GetDiagnostics();
                RawbbitRecoveryProbePlan probe = await restored.PrepareRecoveryProbeAsync(diagnostics.FaultRevision, diagnostics.QueueGeneration);
                Assert.That(probe.Succeeded, Is.True);
                Assert.That(probe.Batch.Records.Single().PayloadJson, Is.EqualTo(fixture.Records.Single().PayloadJson));
                Assert.That(ReadUserField(probe.Batch, "user_pseudo_id"), Is.EqualTo(fixture.InstallationId));
            }
            var adopted = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(adopted.InstallationId, Is.EqualTo(fixture.InstallationId));
            Assert.That(JsonConvert.SerializeObject(adopted.ReadSession()), Is.EqualTo(JsonConvert.SerializeObject(fixture.Session)));
            Assert.That(JsonConvert.SerializeObject(adopted.ReadFault()), Is.EqualTo(JsonConvert.SerializeObject(fixture.Fault)));
            Assert.That(Directory.Exists(legacyRoot), Is.False);
            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Length, Is.EqualTo(2));
        }

        [TestCase(false)]
        [TestCase(true)]
        public void FreshNamespaceRejectsRootFileRegardlessOfLegacyOptIn(bool allowUnresolvedLegacy)
        {
            string root = Path.Combine(_root, "Rawbbit");
            Directory.CreateDirectory(root);
            File.WriteAllBytes(Path.Combine(root, "state.bin"), new byte[] { 0, 255, 13, 10, 128 });
            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = allowUnresolvedLegacy;

            AssertFreshNamespaceRejectedWithoutChanges();
        }

        [TestCase("cache")]
        [TestCase("0000000000000000000000000000000")]
        [TestCase("000000000000000000000000000000000")]
        [TestCase("0000000000000000000000000000000g")]
        [TestCase("ABCDEF0123456789ABCDEF0123456789")]
        [TestCase("store-v2-invalid")]
        [TestCase("store-v2-0000000000000000000000000000000")]
        [TestCase("store-v2-000000000000000000000000000000000")]
        [TestCase("store-v2-0000000000000000000000000000000g")]
        [TestCase("store-v2-ABCDEF0123456789ABCDEF0123456789")]
        public void FreshNamespaceRejectsUnknownDirectoryRegardlessOfLegacyOptIn(string directoryName)
        {
            WriteSiblingMarker(directoryName);
            foreach (bool allowUnresolvedLegacy in new[] { false, true })
            {
                _configuration.AllowFreshStoreWithUnresolvedLegacyStores = allowUnresolvedLegacy;
                AssertFreshNamespaceRejectedWithoutChanges();
            }
        }

        [TestCase(false)]
        [TestCase(true)]
        public void FreshNamespaceLegacyOptInDoesNotBypassMixedUnknownEntries(bool rootFile)
        {
            WriteSiblingMarker("0123456789abcdef0123456789abcdef");
            if (rootFile)
                File.WriteAllBytes(Path.Combine(_root, "Rawbbit", "state.bin"), new byte[] { 0, 255, 128 });
            else
                WriteSiblingMarker("cache");

            foreach (bool allowUnresolvedLegacy in new[] { false, true })
            {
                _configuration.AllowFreshStoreWithUnresolvedLegacyStores = allowUnresolvedLegacy;
                AssertFreshNamespaceRejectedWithoutChanges();
            }
        }

        [TestCase("00000000000000000000000000000000", false)]
        [TestCase("00000000000000000000000000000000", true)]
        [TestCase("0123456789abcdef0123456789abcdef", false)]
        [TestCase("0123456789abcdef0123456789abcdef", true)]
        [TestCase("store-v2-00000000000000000000000000000000", false)]
        [TestCase("store-v2-00000000000000000000000000000000", true)]
        [TestCase("store-v2-0123456789abcdef0123456789abcdef", false)]
        [TestCase("store-v2-0123456789abcdef0123456789abcdef", true)]
        public void FreshNamespaceRecognizesExactHashNamesAndOnlyLegacyRequiresOptIn(string directoryName, bool allowUnresolvedLegacy)
        {
            string siblingRoot = WriteSiblingMarker(directoryName);
            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = allowUnresolvedLegacy;
            if (!directoryName.StartsWith("store-v2-", StringComparison.Ordinal) && !allowUnresolvedLegacy)
            {
                AssertFreshNamespaceRejectedWithoutChanges();
                return;
            }

            string[] siblingEntries = Directory.GetFileSystemEntries(siblingRoot, "*", SearchOption.AllDirectories);
            var files = Directory.GetFiles(siblingRoot, "*", SearchOption.AllDirectories)
                .ToDictionary(path => path, File.ReadAllBytes);
            var fresh = new RawbbitPersistentQueue(_root, _configuration, _clock);

            Guid installationId;
            Assert.That(Guid.TryParse(fresh.InstallationId, out installationId), Is.True);
            Assert.That(fresh.ReadDiagnostics().PendingCount, Is.Zero);
            Assert.That(fresh.ReadSession().SessionId, Is.Null);
            Assert.That(fresh.ReadFault(), Is.Null);
            string identity = _configuration.AppId + "\n" + _configuration.Environment;
            string selectedRoot = Path.Combine(_root, "Rawbbit", "store-v2-" + HashBytes(Encoding.UTF8.GetBytes(identity)).Substring(0, 32));
            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")), Is.EquivalentTo(new[] { siblingRoot, selectedRoot }));
            Assert.That(Directory.GetFileSystemEntries(siblingRoot, "*", SearchOption.AllDirectories), Is.EquivalentTo(siblingEntries));
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value), file.Key);
        }

        [TestCase(false)]
        [TestCase(true)]
        public void UnresolvedLegacyEndpointFailsWithoutCreatingAnotherStore(bool provideWrongPreviousEndpoint)
        {
            string previousUrl = _configuration.CollectorUrl;
            RawbbitPersistentState fixture = WriteLegacyStore(previousUrl, false);
            string legacyRoot = LegacyStoreRoot(previousUrl);
            var files = Directory.GetFiles(legacyRoot).ToDictionary(path => path, File.ReadAllBytes);
            _configuration.CollectorUrl = "https://replacement.example.test";
            if (provideWrongPreviousEndpoint) _configuration.PreviousCollectorUrl = "https://unknown-source.example.test";

            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));

            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")), Is.EqualTo(new[] { legacyRoot }));
            Assert.That(Directory.GetFiles(legacyRoot), Is.EquivalentTo(files.Keys));
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value), file.Key);
            _configuration.PreviousCollectorUrl = previousUrl;
            Assert.That(new RawbbitPersistentQueue(_root, _configuration, _clock).InstallationId, Is.EqualTo(fixture.InstallationId));
        }

        [TestCase(false)]
        [TestCase(true)]
        public void SelectedStoreWithOnlyUnknownStateFileFailsUntouched(bool stableLayout)
        {
            string identity = _configuration.AppId + "\n" + _configuration.Environment;
            string selectedRoot = stableLayout
                ? Path.Combine(_root, "Rawbbit", "store-v2-" + HashBytes(Encoding.UTF8.GetBytes(identity)).Substring(0, 32))
                : LegacyStoreRoot(_configuration.CollectorUrl);
            Directory.CreateDirectory(selectedRoot);
            string path = Path.Combine(selectedRoot, "state.json");
            byte[] contents = Encoding.UTF8.GetBytes("{\"installation_id\":\"unknown-layout-identity\",\"events\":[\"preserve-me\"]}");
            File.WriteAllBytes(path, contents);

            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = true;
            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));

            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")), Is.EqualTo(new[] { selectedRoot }));
            Assert.That(Directory.GetFiles(selectedRoot, "*", SearchOption.AllDirectories), Is.EqualTo(new[] { path }));
            Assert.That(File.ReadAllBytes(path), Is.EqualTo(contents));
        }

        [TestCase(false)]
        [TestCase(true)]
        public void InterruptedInitialCheckpointTempDoesNotPermanentlyBlockOpen(bool legacyLayout)
        {
            string identity = _configuration.AppId + "\n" + _configuration.Environment;
            string selectedRoot = legacyLayout ? LegacyStoreRoot(_configuration.CollectorUrl)
                : Path.Combine(_root, "Rawbbit", "store-v2-" + HashBytes(Encoding.UTF8.GetBytes(identity)).Substring(0, 32));
            Directory.CreateDirectory(selectedRoot);
            string temp = Path.Combine(selectedRoot, "snapshot-00000000000000000000.json." + Guid.NewGuid().ToString("N") + ".tmp");
            File.WriteAllText(temp, "{\"FormatVersion\":"); // Process died during first checkpoint write.

            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(queue.InstallationId, Is.Not.Null.And.Not.Empty);
            Assert.That(queue.ReadDiagnostics().PendingCount, Is.Zero);
            Assert.That(Directory.GetFiles(Path.Combine(_root, "Rawbbit"), "*.tmp", SearchOption.AllDirectories), Is.Empty);
            Assert.That(new RawbbitPersistentQueue(_root, _configuration, _clock).InstallationId, Is.EqualTo(queue.InstallationId));
        }

        [TestCase("mixed_unknown")]
        [TestCase("invalid_guid")]
        [TestCase("later_checkpoint")]
        public void UnknownOrLaterCheckpointTempsAreNotCleared(string contents)
        {
            _configuration.AllowFreshStoreWithUnresolvedLegacyStores = true;
            string selectedRoot = LegacyStoreRoot(_configuration.CollectorUrl);
            Directory.CreateDirectory(selectedRoot);
            string sequence = contents == "later_checkpoint" ? "00000000000000000001" : "00000000000000000000";
            string identifier = contents == "invalid_guid" ? "unknown" : Guid.NewGuid().ToString("N");
            File.WriteAllText(Path.Combine(selectedRoot, "snapshot-" + sequence + ".json." + identifier + ".tmp"), "preserve-temp");
            if (contents == "mixed_unknown") File.WriteAllText(Path.Combine(selectedRoot, "state.json"), "preserve-unknown-state");
            var files = Directory.GetFiles(selectedRoot).ToDictionary(path => path, File.ReadAllBytes);

            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));
            Assert.That(Directory.GetFiles(selectedRoot), Is.EquivalentTo(files.Keys));
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value));
        }

        [Test]
        public async Task EndpointMoveInvalidatesValidatedProbeAndEmptyQueueCannotClearFault()
        {
            string previousUrl = _configuration.CollectorUrl;
            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            var client = new RawbbitClient(_configuration, queue, _clock);
            await client.ConfirmConsentAsync();
            await client.TrackAsync(Event("accepted_probe_before_endpoint_move"));
            queue.LatchFault("collector_auth_or_route_failure", 401, _clock.UtcNow, true);
            RawbbitQueueDiagnostics before = queue.ReadDiagnostics();
            RawbbitRecoveryProbePlan probe = await client.PrepareRecoveryProbeAsync(before.FaultRevision, before.QueueGeneration);
            Assert.That(probe.Succeeded, Is.True);
            AssertDispatch(client, probe.Batch, true);
            Assert.That((await client.CompleteRecoveryProbeAsync(probe.Batch, new RawbbitTransportResult
            {
                StatusCode = 200,
                ResponseBody = "{\"accepted_events\":1}"
            })).Succeeded, Is.True);
            RawbbitFaultState validatedFault = queue.ReadFault();
            Assert.That(validatedFault.ProbeValidated, Is.True);
            Assert.That(queue.ReadDiagnostics().PendingCount, Is.Zero);
            string installationId = queue.InstallationId;
            string sessionJson = JsonConvert.SerializeObject(queue.ReadSession());
            await client.DisposeClient();

            _configuration.CollectorUrl = "https://replacement.example.test";
            _configuration.PreviousCollectorUrl = previousUrl;
            var migratedQueue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(migratedQueue.InstallationId, Is.EqualTo(installationId));
            Assert.That(JsonConvert.SerializeObject(migratedQueue.ReadSession()), Is.EqualTo(sessionJson));
            AssertFaultAfterEndpointBinding(validatedFault, migratedQueue.ReadFault(), true);
            _configuration.PreviousCollectorUrl = null;
            var restarted = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(JsonConvert.SerializeObject(restarted.ReadFault()), Is.EqualTo(JsonConvert.SerializeObject(migratedQueue.ReadFault())));
            using (var migrated = new RawbbitClient(_configuration, restarted, _clock))
            {
                await migrated.ConfirmConsentAsync();
                RawbbitQueueDiagnostics diagnostics = migrated.GetDiagnostics();
                Assert.That(diagnostics.RecoveryProbeRequired, Is.True);
                Assert.That(diagnostics.RecoveryProbeValidated, Is.False);
                RawbbitRecoveryResult resume = await migrated.ValidateAndResumeAsync(diagnostics.FaultRevision,
                    diagnostics.QueueGeneration, "configuration_corrected", "test_policy_v1");
                Assert.That(resume.Succeeded, Is.False);
                Assert.That(resume.SafeDiagnosticCode, Is.EqualTo("recovery_probe_required"));
                RawbbitRecoveryProbePlan emptyProbe = await migrated.PrepareRecoveryProbeAsync(diagnostics.FaultRevision, diagnostics.QueueGeneration);
                Assert.That(emptyProbe.Succeeded, Is.False);
                Assert.That(emptyProbe.SafeDiagnosticCode, Is.EqualTo("recovery_probe_pending_event_required"));
                Assert.That(migrated.GetDiagnostics().FaultRevision, Is.EqualTo(diagnostics.FaultRevision));

                Assert.That((await migrated.TrackAsync(Event("probe_for_new_endpoint"))).IsDurablyStored, Is.True);
                diagnostics = migrated.GetDiagnostics();
                RawbbitRecoveryProbePlan newProbe = await migrated.PrepareRecoveryProbeAsync(diagnostics.FaultRevision, diagnostics.QueueGeneration);
                Assert.That(newProbe.Succeeded, Is.True);
                Assert.That(newProbe.Batch.Endpoint, Is.EqualTo("https://replacement.example.test/v1/events:batch"));
                AssertDispatch(migrated, newProbe.Batch, true);
            }
        }

        [TestCase(true)]
        [TestCase(false)]
        public async Task StableStoreIsIsolatedByAppAndEnvironment(bool changeApp)
        {
            RawbbitClient original = await Open();
            await original.ConfirmConsentAsync();
            await original.TrackAsync(Event("isolated_original"));
            RawbbitPreparedBatch originalBatch = original.TryBeginRequest();
            await original.DisposeClient();
            string appId = _configuration.AppId;
            string environment = _configuration.Environment;
            if (changeApp) _configuration.AppId = "other.game";
            else _configuration.Environment = "other_environment";
            using (RawbbitClient isolated = await Open())
            {
                Assert.That(isolated.GetDiagnostics().PendingCount, Is.Zero);
                await isolated.ConfirmConsentAsync();
                await isolated.TrackAsync(Event("isolated_other"));
                Assert.That(ReadUserField(isolated.TryBeginRequest(), "user_pseudo_id"), Is.Not.EqualTo(ReadUserField(originalBatch, "user_pseudo_id")));
            }
            Assert.That(Directory.GetDirectories(Path.Combine(_root, "Rawbbit")).Length, Is.EqualTo(2));
            _configuration.AppId = appId;
            _configuration.Environment = environment;
            using (RawbbitClient restored = await Open())
            {
                await restored.ConfirmConsentAsync();
                Assert.That(restored.TryBeginRequest().Body, Is.EqualTo(originalBatch.Body));
            }
        }

        [TestCase(false)]
        [TestCase(true)]
        public async Task MigratedLegacyQueueStillExpiresAndCanBePurged(bool purge)
        {
            string previousUrl = _configuration.CollectorUrl;
            RawbbitPersistentState fixture = WriteLegacyStore(previousUrl, false);
            _configuration.CollectorUrl = "https://replacement.example.test";
            _configuration.PreviousCollectorUrl = previousUrl;
            _clock.UtcNow = _clock.UtcNow.AddDays(7).AddTicks(-1);
            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            using (var client = new RawbbitClient(_configuration, queue, _clock))
            {
                await client.ConfirmConsentAsync();
                Assert.That(client.TryBeginRequest(), Is.Not.Null);
                if (purge)
                {
                    Assert.That((await client.WithdrawConsentAsync()).Succeeded, Is.True);
                    Assert.That(queue.ReadSession().SessionId, Is.Null);
                    foreach (string file in Directory.GetFiles(Path.Combine(_root, "Rawbbit"), "*", SearchOption.AllDirectories))
                        Assert.That(Encoding.UTF8.GetString(File.ReadAllBytes(file)), Does.Not.Contain("legacy_frozen_event"), file);
                }
                else
                {
                    _clock.UtcNow = _clock.UtcNow.AddTicks(1);
                    Assert.That(client.TryBeginRequest(), Is.Null);
                    Assert.That(client.GetDiagnostics().ExpiredCount, Is.EqualTo(1));
                }
                Assert.That(client.GetDiagnostics().PendingCount, Is.Zero);
            }
            _configuration.PreviousCollectorUrl = null;
            var restarted = new RawbbitPersistentQueue(_root, _configuration, _clock);
            Assert.That(restarted.InstallationId, Is.EqualTo(fixture.InstallationId));
            Assert.That(restarted.ReadDiagnostics().PendingCount, Is.Zero);
            Assert.That(restarted.ReadDiagnostics().ExpiredCount, Is.EqualTo(purge ? 0 : 1));
        }

        [TestCase("expiry")]
        [TestCase("eviction")]
        [TestCase("acknowledgement")]
        [TestCase("quarantine")]
        [TestCase("auth_fault")]
        [TestCase("storage_failure")]
        public async Task PreparedDispatchRechecksActualQueueBeforeStartingTransport(string mutation)
        {
            _configuration.MaxQueueBytes = 2048;
            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            using (var client = new RawbbitClient(_configuration, queue, _clock))
            {
                await client.ConfirmConsentAsync();
                await client.TrackAsync(Event("prepared_before_mutation"));
                RawbbitPreparedBatch prepared = client.TryBeginRequest();
                Assert.That(prepared, Is.Not.Null);
                switch (mutation)
                {
                    case "expiry":
                        _clock.UtcNow = _clock.UtcNow.AddDays(7);
                        Assert.That(queue.ReadDiagnostics().PendingCount, Is.EqualTo(1), "No prune has run since preparation.");
                        break;
                    case "eviction":
                        for (int index = 0; index < 4; index++)
                            Assert.That((await client.TrackAsync(Event("replacement_" + index))).IsDurablyStored, Is.True);
                        Assert.That(queue.ReadDiagnostics().EvictedCount, Is.GreaterThan(0));
                        Assert.That(queue.GetPendingRecords(500, _configuration.MaxRequestBytes, _clock.UtcNow).Select(record => record.Handle), Does.Not.Contain(prepared.Handles.Single()));
                        break;
                    case "acknowledgement":
                        queue.Acknowledge(prepared.Handles);
                        break;
                    case "quarantine":
                        queue.Quarantine(prepared.Handles, 422, "collector_http_422", _clock.UtcNow, true);
                        Assert.That(queue.ReadDiagnostics().QuarantinedCount, Is.EqualTo(1));
                        break;
                    case "auth_fault":
                        queue.LatchFault("collector_auth_or_route_failure", 401, _clock.UtcNow, true);
                        break;
                    case "storage_failure":
                        string journal = Directory.GetFiles(Path.Combine(_root, "Rawbbit"), "journal-*.bin", SearchOption.AllDirectories).Single();
                        File.Delete(journal);
                        Directory.CreateDirectory(journal);
                        // Opening a directory as a journal can raise IOException or
                        // UnauthorizedAccessException depending on the Unity runtime.
                        Assert.Catch(() => queue.Acknowledge(prepared.Handles));
                        Assert.That(queue.IsStorageFaulted, Is.True);
                        Assert.That(queue.ReadDiagnostics().PendingCount, Is.EqualTo(1));
                        break;
                }
                AssertDispatch(client, prepared, false);
                if (mutation == "expiry")
                {
                    Assert.That(queue.ReadDiagnostics().PendingCount, Is.EqualTo(1));
                    Assert.That(queue.ReadDiagnostics().ExpiredCount, Is.Zero, "Dispatch validation must not write a prune transaction.");
                }
            }
        }

        [TestCase(false)]
        [TestCase(true)]
        public async Task UnchangedPreparedBatchDispatchesEvenWhenUnrelatedEventIsEnqueued(bool enqueueAnother)
        {
            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            using (var client = new RawbbitClient(_configuration, queue, _clock))
            {
                await client.ConfirmConsentAsync();
                await client.TrackAsync(Event("dispatch_original"));
                RawbbitPreparedBatch prepared = client.TryBeginRequest();
                if (enqueueAnother) await client.TrackAsync(Event("dispatch_later"));
                AssertDispatch(client, prepared, true);
                await client.CompleteRequestAsync(prepared, new RawbbitTransportResult { StatusCode = 200, ResponseBody = "{\"accepted_events\":1}" });
                Assert.That(queue.ReadDiagnostics().PendingCount, Is.EqualTo(enqueueAnother ? 1 : 0));
            }
        }

        [TestCase("unchanged")]
        [TestCase("fault_revision")]
        [TestCase("queue_generation")]
        public async Task RecoveryProbeDispatchRequiresCurrentFaultAndQueueRevisions(string mutation)
        {
            var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
            using (var client = new RawbbitClient(_configuration, queue, _clock))
            {
                await client.ConfirmConsentAsync();
                await client.TrackAsync(Event("probe_original"));
                queue.LatchFault("collector_auth_or_route_failure", 401, _clock.UtcNow, true);
                RawbbitQueueDiagnostics before = queue.ReadDiagnostics();
                RawbbitRecoveryProbePlan probe = await client.PrepareRecoveryProbeAsync(before.FaultRevision, before.QueueGeneration);
                Assert.That(probe.Succeeded, Is.True);
                Assert.That(probe.Batch, Is.Not.Null);
                if (mutation == "fault_revision")
                {
                    queue.LatchFault("collector_auth_or_route_failure", 403, _clock.UtcNow, true);
                    Assert.That(queue.ReadDiagnostics().FaultRevision, Is.Not.EqualTo(before.FaultRevision));
                    Assert.That(queue.ReadDiagnostics().QueueGeneration, Is.EqualTo(before.QueueGeneration));
                }
                else if (mutation == "queue_generation")
                {
                    Assert.That((await client.TrackAsync(Event("probe_later"))).IsDurablyStored, Is.True);
                    Assert.That(queue.ReadDiagnostics().QueueGeneration, Is.Not.EqualTo(before.QueueGeneration));
                    Assert.That(queue.ReadDiagnostics().FaultRevision, Is.EqualTo(before.FaultRevision));
                }
                AssertDispatch(client, probe.Batch, mutation == "unchanged");
            }
        }

        [Test]
        public async Task DispatchDefersPromptlyWhileWorkerHoldsSessionFactoryGateThenSucceeds()
        {
            using (var factoryStarted = new ManualResetEventSlim(false))
            using (var releaseFactory = new ManualResetEventSlim(false))
            {
                int factoryCalls = 0;
                RawbbitSessionStartFactory factory = (context, triggering) =>
                {
                    if (Interlocked.Increment(ref factoryCalls) == 2)
                    {
                        factoryStarted.Set();
                        if (!releaseFactory.Wait(TimeSpan.FromSeconds(10)))
                            throw new TimeoutException("Test session factory timed out.");
                    }
                    return null;
                };
                var queue = new RawbbitPersistentQueue(_root, _configuration, _clock);
                var client = new RawbbitClient(_configuration, queue, _clock, null, factory);
                Task<RawbbitTrackResult> tracking = null;
                Task dispatch = null;
                try
                {
                    await client.ConfirmConsentAsync();
                    Assert.That((await client.TrackAsync(Event("prepared_before_busy_worker"))).IsDurablyStored, Is.True);
                    RawbbitPreparedBatch prepared = client.TryBeginRequest();
                    Assert.That(prepared, Is.Not.Null);
                    _clock.UtcNow = _clock.UtcNow.AddMinutes(31);
                    tracking = client.TrackAsync(Event("new_session_holds_worker_gate"));
                    Assert.That(await Task.Run(() => factoryStarted.Wait(TimeSpan.FromSeconds(5))), Is.True);

                    // Run the potentially regressed blocking call off Unity's test
                    // thread; the deadline and finally release keep failures bounded.
                    dispatch = Task.Run(() => AssertDispatch(client, prepared, false));
                    Assert.That(await Task.WhenAny(dispatch, Task.Delay(TimeSpan.FromSeconds(2))), Is.SameAs(dispatch),
                        "A busy worker must defer dispatch rather than block waiting for the factory.");
                    await dispatch;
                    Assert.That(releaseFactory.IsSet, Is.False);
                    releaseFactory.Set();
                    Assert.That((await tracking).IsDurablyStored, Is.True);
                    AssertDispatch(client, prepared, true);
                    Assert.That(client.TryBeginRequest(), Is.Not.Null);
                    Assert.That(client.GetDiagnostics().FaultCode, Is.Null);
                }
                finally
                {
                    releaseFactory.Set();
                    if (tracking != null) await tracking;
                    await client.DisposeClient();
                    if (dispatch != null) await dispatch;
                }
            }
        }

        private static void AssertFaultAfterEndpointBinding(RawbbitFaultState previous, RawbbitFaultState current, bool endpointChanged)
        {
            Assert.That(current, Is.Not.Null);
            Assert.That(current.FaultCode, Is.EqualTo(previous.FaultCode));
            Assert.That(current.HttpStatusCode, Is.EqualTo(previous.HttpStatusCode));
            Assert.That(current.CreatedUtcTicks, Is.EqualTo(previous.CreatedUtcTicks));
            Assert.That(current.RequiresProbe, Is.EqualTo(previous.RequiresProbe));
            Assert.That(current.ProbeValidated, Is.EqualTo(endpointChanged ? false : previous.ProbeValidated));
            if (endpointChanged) Assert.That(current.Revision, Is.GreaterThan(previous.Revision));
            else Assert.That(current.Revision, Is.EqualTo(previous.Revision));
        }

        private static void AssertDispatch(RawbbitClient client, RawbbitPreparedBatch prepared, bool expected)
        {
            int starts = 0;
            Task<RawbbitTransportResult> request;
            Task<RawbbitTransportResult> response = Task.FromResult(new RawbbitTransportResult { StatusCode = 200 });
            bool dispatched = client.TryStartDispatch(prepared, () =>
            {
                starts++;
                return response;
            }, out request);
            Assert.That(dispatched, Is.EqualTo(expected));
            Assert.That(starts, Is.EqualTo(expected ? 1 : 0));
            if (expected) Assert.That(request, Is.SameAs(response));
            else Assert.That(request, Is.Null);
        }

        private string WriteSiblingMarker(string directoryName)
        {
            string siblingRoot = Path.Combine(_root, "Rawbbit", directoryName);
            string nestedRoot = Path.Combine(siblingRoot, "nested");
            Directory.CreateDirectory(Path.Combine(nestedRoot, "empty"));
            // Sibling recognition checks names, not the validity of their contents.
            File.WriteAllBytes(Path.Combine(nestedRoot, "marker.bin"), new byte[] { 0, 255, 13, 10, 128 });
            return siblingRoot;
        }

        private void AssertFreshNamespaceRejectedWithoutChanges()
        {
            string root = Path.Combine(_root, "Rawbbit");
            string[] entries = Directory.GetFileSystemEntries(root, "*", SearchOption.AllDirectories);
            var files = Directory.GetFiles(root, "*", SearchOption.AllDirectories)
                .ToDictionary(path => path, File.ReadAllBytes);

            Assert.Throws<InvalidDataException>(() => new RawbbitPersistentQueue(_root, _configuration, _clock));

            Assert.That(Directory.GetFileSystemEntries(root, "*", SearchOption.AllDirectories), Is.EquivalentTo(entries),
                "Rejected initialization must not create a store or change existing entries.");
            foreach (var file in files) Assert.That(File.ReadAllBytes(file.Key), Is.EqualTo(file.Value), file.Key);
        }

        private RawbbitPersistentState WriteLegacyStore(string collectorUrl, bool faulted)
        {
            var state = new RawbbitPersistentState
            {
                Sequence = 1,
                QueueGeneration = 1,
                InstallationId = Guid.NewGuid().ToString("D"),
                Session = new RawbbitSessionState
                {
                    SessionId = Guid.NewGuid().ToString("D"),
                    LastActivityUtcTicks = _clock.UtcNow.UtcDateTime.Ticks,
                    EventCounter = 1
                }
            };
            string eventId = Guid.NewGuid().ToString("D");
            var record = new RawbbitQueueRecord
            {
                Handle = Guid.NewGuid().ToString("N"),
                EventId = eventId,
                PayloadJson = RawbbitPayload.BuildEventJson(Event("legacy_frozen_event"), _configuration, state.InstallationId, state.Session.SessionId, eventId, _clock.UtcNow),
                EnqueuedUtcTicks = _clock.UtcNow.UtcDateTime.Ticks,
                Sequence = 1
            };
            record.QueueBytes = RawbbitPayload.MeasureRecordBytes(record);
            state.Records.Add(record);
            if (faulted)
                state.Fault = new RawbbitFaultState { FaultCode = "collector_auth_or_route_failure", HttpStatusCode = 401, Revision = 1, CreatedUtcTicks = _clock.UtcNow.UtcDateTime.Ticks, RequiresProbe = true };
            string root = LegacyStoreRoot(collectorUrl);
            Directory.CreateDirectory(root);
            JObject legacyState = JObject.FromObject(state);
            legacyState.Remove("CollectorEndpoint");
            string stateJson = legacyState.ToString(Formatting.None);
            var envelope = new RawbbitSnapshotEnvelope { FormatVersion = 1, StateJson = stateJson, Sha256 = HashBytes(Encoding.UTF8.GetBytes(stateJson)) };
            File.WriteAllText(Path.Combine(root, "snapshot-00000000000000000001.json"), JsonConvert.SerializeObject(envelope));

            // A real v1 journal frame exercises replay as well as checkpoint adoption.
            state.Session.EventCounter = 2;
            var transaction = new RawbbitStoreTransaction { Sequence = 2, SetSession = true, Session = state.Session };
            JObject legacyTransaction = JObject.FromObject(transaction);
            legacyTransaction.Remove("SetCollectorEndpoint");
            legacyTransaction.Remove("CollectorEndpoint");
            byte[] payload = Encoding.UTF8.GetBytes(legacyTransaction.ToString(Formatting.None));
            using (var stream = new FileStream(Path.Combine(root, "journal-00000000000000000001.bin"), FileMode.CreateNew))
            using (var writer = new BinaryWriter(stream))
            using (var sha = SHA256.Create())
            {
                writer.Write(payload.Length);
                writer.Write(sha.ComputeHash(payload));
                writer.Write(payload);
            }
            state.Sequence = 2;
            return state;
        }

        private string LegacyStoreRoot(string collectorUrl)
        {
            string endpoint = collectorUrl.Trim().TrimEnd('/');
            if (!endpoint.EndsWith("/v1/events:batch", StringComparison.OrdinalIgnoreCase)) endpoint += "/v1/events:batch";
            string identity = endpoint + "\n" + _configuration.AppId + "\n" + _configuration.Environment;
            return Path.Combine(_root, "Rawbbit", HashBytes(Encoding.UTF8.GetBytes(identity)).Substring(0, 32));
        }

        private static string HashBytes(byte[] bytes)
        {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        }

        private Task<RawbbitClient> Open()
        {
            return RawbbitClient.OpenAsync(_configuration, _root, _clock);
        }

        private static RawbbitEventInput Event(string name)
        {
            return new RawbbitEventInput
            {
                EventName = name,
                EventParams = new Dictionary<string, object>(),
                UserProperties = new Dictionary<string, object>()
            };
        }

        private static string ReadUserField(RawbbitPreparedBatch batch, string field)
        {
            JObject body = JObject.Parse(batch.Body);
            return body["events"][0]["user"][field].Value<string>();
        }

        private sealed class FakeClock : IRawbbitClock
        {
            public DateTimeOffset UtcNow { get; set; }
        }

        private sealed class BlockingIdentityProvider : IRawbbitIdentityProvider, IDisposable
        {
            internal readonly ManualResetEventSlim Started = new ManualResetEventSlim(false);
            internal readonly ManualResetEventSlim Continue = new ManualResetEventSlim(false);

            public string GetUserPseudoId()
            {
                Started.Set();
                if (!Continue.Wait(TimeSpan.FromSeconds(5))) throw new TimeoutException("Test identity provider timed out.");
                return "approved-test-pseudo-id";
            }

            public string GetUserId() { return null; }

            public void Dispose()
            {
                Started.Dispose();
                Continue.Dispose();
            }
        }
    }
}
