"""General application settings."""
CHAT_LIMIT = 100
# Retry/backoff policy for the Supervisor (agenten/supervision/supervisor.py):
# a failed subproblem is retried up to max_retries times, with the delay
# before attempt N being backoff_initial_delay_seconds * backoff_base ** N.
RETRY_POLICY = {
    "max_retries": 3,
    "backoff_base": 2.0,
    "backoff_initial_delay_seconds": 1.0,
}
