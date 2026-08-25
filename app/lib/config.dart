/// Connection settings for the localhost backend.
///
/// Phase 0 keeps this simple and hardcoded to the FastAPI defaults.
/// Override with `--dart-define=JARVIS_WS_URL=ws://127.0.0.1:9000/ws/dev`
/// if you run the backend elsewhere.
library;

const String kDefaultWsUrl = String.fromEnvironment(
  'JARVIS_WS_URL',
  defaultValue: 'ws://127.0.0.1:8000/ws/desktop',
);
