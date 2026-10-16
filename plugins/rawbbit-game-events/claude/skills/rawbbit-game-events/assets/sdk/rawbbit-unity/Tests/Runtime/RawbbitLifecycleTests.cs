using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Threading.Tasks;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

namespace Rawbbit.Unity.Tests
{
    public sealed class RawbbitLifecycleTests
    {
        [UnityTest]
        public IEnumerator HostRequiresFreshConsentAndUsesInjectedTransportAfterGrant()
        {
            string appId = "test.playmode." + System.Guid.NewGuid().ToString("N");
            string root = Path.Combine(Path.GetTempPath(), "rawbbit-unity-playmode", System.Guid.NewGuid().ToString("N"));
            var transport = new FakeTransport();
            var host = RawbbitUnityHost.CreateForTests(new RawbbitConfiguration
            {
                CollectorUrl = "https://collector.example.test",
                ApiKey = "test-only-not-a-real-key",
                AppId = appId,
                Environment = "test",
                Platform = "unity_editor"
            }, root, transport);
            yield return new WaitUntil(() => host.IsReady || host.InitializationFailed);
            Assert.That(host.IsReady, Is.True);
            Assert.That(host.Client.HasCurrentConsent, Is.False);
            Assert.That(host.Client.TryBeginRequest(System.DateTimeOffset.UtcNow), Is.Null);
            Assert.That(transport.SendCount, Is.Zero);

            Task<RawbbitRecoveryResult> grant = host.ConfirmConsentAsync();
            yield return new WaitUntil(() => grant.IsCompleted);
            Assert.That(grant.Result.Succeeded, Is.True);
            Task<RawbbitTrackResult> track = host.TrackAsync(new RawbbitEventInput
            {
                EventName = "neutral_test_event",
                EventParams = new Dictionary<string, object>(),
                UserProperties = new Dictionary<string, object>()
            });
            yield return new WaitUntil(() => track.IsCompleted);
            Assert.That(track.Result.Status, Is.EqualTo(RawbbitTrackStatus.DurablyStored));
            yield return new WaitUntil(() => transport.SendCount == 1 && host.Client.GetDiagnostics().PendingCount == 0);
            Assert.That(transport.LastBody, Does.Contain("neutral_test_event"));
            Object.Destroy(host.gameObject);
            yield return null;
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }

        private sealed class FakeTransport : IRawbbitTransport
        {
            internal int SendCount;
            internal string LastBody;

            public Task<RawbbitTransportResult> SendAsync(RawbbitPreparedBatch batch)
            {
                SendCount++;
                LastBody = batch.Body;
                return Task.FromResult(new RawbbitTransportResult
                {
                    StatusCode = 200,
                    NetworkError = false,
                    ResponseBody = "{\"request_id\":\"test\",\"accepted_events\":" + batch.Records.Count + "}"
                });
            }

            public void Abort()
            {
            }
        }
    }
}
