import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/health.dart';

void main() {
  group('BackendHealth.fromJson', () {
    test('reads the fields the settings sheet shows', () {
      final health = BackendHealth.fromJson(const {
        'status': 'ok',
        'provider': 'ollama',
        'model': 'qwen3:4b',
        'ollama_base_url': 'http://localhost:11434',
        'whisper_model': 'small',
        'tts_voice': 'af_heart',
        'wake_enabled': true,
        'tools': ['system_stats', 'read_file'],
        'tool_risks': {'system_stats': 'read-only', 'read_file': 'read-only'},
        'audit_path': '.jarvis_audit.jsonl',
      });

      expect(health.label, 'qwen3:4b');
      expect(health.toolCount, 2);
      expect(health.risks['read_file'], 'read-only');
      expect(health.auditPath, '.jarvis_audit.jsonl');
      expect(health.isEmpty, isFalse);
    });

    test('degrades instead of throwing on a malformed payload', () {
      final health = BackendHealth.fromJson(const {
        'model': 42,
        'tools': 'not-a-list',
        'tool_risks': ['nope'],
        'wake_enabled': 'yes please',
      });

      expect(health.model, '');
      expect(health.tools, isEmpty);
      expect(health.risks, isEmpty);
      // Anything but an explicit false keeps the wake word switched on.
      expect(health.wakeEnabled, isTrue);
      expect(health.label, 'local');
    });

    test('label falls back to the provider when the model is unset', () {
      expect(
        BackendHealth.fromJson(const {'provider': 'anthropic'}).label,
        'anthropic',
      );
    });

    test('an empty object is treated as no information', () {
      expect(BackendHealth.fromJson(const {}).isEmpty, isTrue);
    });
  });
}
