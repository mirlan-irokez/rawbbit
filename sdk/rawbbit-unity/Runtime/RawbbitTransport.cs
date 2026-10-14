using System;
using System.Globalization;
using System.Threading.Tasks;
using UnityEngine.Networking;

namespace Rawbbit.Unity
{
    internal interface IRawbbitTransport
    {
        Task<RawbbitTransportResult> SendAsync(RawbbitPreparedBatch batch);
        void Abort();
    }

    internal sealed class RawbbitTransport : IRawbbitTransport
    {
        private UnityWebRequest _activeRequest;

        public Task<RawbbitTransportResult> SendAsync(RawbbitPreparedBatch batch)
        {
            if (batch == null) throw new ArgumentNullException("batch");
            if (_activeRequest != null) throw new InvalidOperationException("A UnityWebRequest is already active.");
            UnityWebRequest request = null;
            var completion = new TaskCompletionSource<RawbbitTransportResult>();
            UnityWebRequestAsyncOperation operation;
            try
            {
                byte[] body = System.Text.Encoding.UTF8.GetBytes(batch.Body);
                request = new UnityWebRequest(batch.Endpoint, UnityWebRequest.kHttpVerbPOST)
                {
                    uploadHandler = new UploadHandlerRaw(body),
                    downloadHandler = new DownloadHandlerBuffer(),
                    timeout = batch.TimeoutSeconds,
                    redirectLimit = 0
                };
                request.SetRequestHeader("Content-Type", "application/json");
                request.SetRequestHeader("X-API-Key", batch.ApiKey);
                _activeRequest = request;
                operation = request.SendWebRequest();
            }
            catch
            {
                _activeRequest = null;
                if (request != null) request.Dispose();
                return Task.FromResult(new RawbbitTransportResult { StatusCode = 0, NetworkError = true });
            }

            int completed = 0;
            Action<UnityEngine.AsyncOperation> finish = delegate
            {
                if (System.Threading.Interlocked.Exchange(ref completed, 1) != 0) return;
                RawbbitTransportResult result;
                try { result = ReadResult(request); }
                catch { result = new RawbbitTransportResult { StatusCode = (int)request.responseCode, NetworkError = true }; }
                _activeRequest = null;
                request.Dispose();
                completion.TrySetResult(result);
            };
            operation.completed += finish;
            if (operation.isDone) finish(operation);
            return completion.Task;
        }

        public void Abort()
        {
            if (_activeRequest != null && !_activeRequest.isDone) _activeRequest.Abort();
        }

        private static RawbbitTransportResult ReadResult(UnityWebRequest request)
        {
            int? retryAfter = ParseRetryAfter(request.GetResponseHeader("Retry-After"));
            string responseBody = null;
            if (request.downloadHandler != null)
            {
                string body = request.downloadHandler.text;
                if (body != null && body.Length <= 4096) responseBody = body;
            }
            return new RawbbitTransportResult
            {
                StatusCode = (int)request.responseCode,
                NetworkError = request.result == UnityWebRequest.Result.ConnectionError,
                ResponseBody = responseBody,
                RetryAfterSeconds = retryAfter
            };
        }

        internal static int? ParseRetryAfter(string header)
        {
            if (String.IsNullOrWhiteSpace(header)) return null;
            string value = header.Trim();
            bool decimalSeconds = value.Length > 0;
            for (int i = 0; i < value.Length && decimalSeconds; i++)
                decimalSeconds = value[i] >= '0' && value[i] <= '9';
            if (decimalSeconds)
            {
                long seconds;
                if (!Int64.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out seconds)) return 3600;
                return (int)Math.Min(3600L, seconds);
            }
            DateTimeOffset retryAt;
            if (DateTimeOffset.TryParse(header, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out retryAt))
            {
                double secondsUntil = Math.Ceiling((retryAt - DateTimeOffset.UtcNow).TotalSeconds);
                if (secondsUntil <= 0) return 0;
                if (secondsUntil >= 3600) return 3600;
                return (int)secondsUntil;
            }
            return null;
        }
    }
}
