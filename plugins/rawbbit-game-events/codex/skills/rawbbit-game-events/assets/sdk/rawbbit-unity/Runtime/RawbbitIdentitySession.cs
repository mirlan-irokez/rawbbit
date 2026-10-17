using System;

namespace Rawbbit.Unity
{
    internal static class RawbbitIdentitySession
    {
        internal static SessionDecision Decide(RawbbitSessionState previous, DateTimeOffset now, TimeSpan timeout, bool forceNewSession, Func<string> sessionIdFactory = null)
        {
            long nowTicks = now.UtcDateTime.Ticks;
            bool hasSession = previous != null && !String.IsNullOrWhiteSpace(previous.SessionId);
            bool expired = hasSession && nowTicks > previous.LastActivityUtcTicks && nowTicks - previous.LastActivityUtcTicks > timeout.Ticks;
            bool create = forceNewSession || !hasSession || expired;
            string sessionId = create ? CreateSessionId(sessionIdFactory) : previous.SessionId;
            if (create && hasSession && String.Equals(sessionId, previous.SessionId, StringComparison.Ordinal))
                throw new ArgumentException("A new session must use a different session ID.");
            if (!create && previous.EventCounter == Int32.MaxValue)
                throw new InvalidOperationException("The persisted session event counter is exhausted.");
            int sequence = create ? 1 : Math.Max(0, previous.EventCounter) + 1;

            // Wall clock rollback must not make persisted activity move backward
            // or create a negative inactivity interval across process restarts.
            long activityTicks = hasSession && nowTicks < previous.LastActivityUtcTicks
                ? previous.LastActivityUtcTicks
                : nowTicks;

            return new SessionDecision
            {
                State = new RawbbitSessionState
                {
                    SessionId = sessionId,
                    LastActivityUtcTicks = activityTicks,
                    EventCounter = sequence
                },
                IsNewSession = create,
                TriggerSequence = sequence,
                SessionStartSequence = create ? 1 : 0
            };
        }

        internal static RawbbitSessionState Empty()
        {
            return new RawbbitSessionState { SessionId = null, LastActivityUtcTicks = 0, EventCounter = 0 };
        }

        private static string CreateSessionId(Func<string> sessionIdFactory)
        {
            string value = sessionIdFactory == null ? Guid.NewGuid().ToString("D") : sessionIdFactory();
            if (String.IsNullOrWhiteSpace(value) || value.Length > 128)
                throw new ArgumentException("The session ID provider must return a non-empty value of at most 128 characters.");
            for (int i = 0; i < value.Length; i++) if (Char.IsControl(value[i])) throw new ArgumentException("The session ID provider returned a control character.");
            return value;
        }
    }

    internal sealed class SessionDecision
    {
        internal RawbbitSessionState State;
        internal bool IsNewSession;
        internal int TriggerSequence;
        internal int SessionStartSequence;
    }
}
