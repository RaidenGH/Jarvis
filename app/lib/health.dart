/// The backend's `GET /health` answer, parsed.
///
/// The shell shows this in the settings sheet (and the model name in the
/// status chip), so the parsing is defensive: a missing or oddly-typed field
/// degrades to a blank rather than throwing at the UI.
class BackendHealth {
  const BackendHealth({
    this.status = '',
    this.provider = '',
    this.model = '',
    this.ollamaBaseUrl = '',
    this.whisperModel = '',
    this.ttsVoice = '',
    this.wakeEnabled = true,
    this.tools = const [],
    this.risks = const {},
    this.auditPath = '',
  });

  static const empty = BackendHealth();

  final String status;
  final String provider;
  final String model;
  final String ollamaBaseUrl;
  final String whisperModel;
  final String ttsVoice;
  final bool wakeEnabled;
  final List<String> tools;

  /// Tool name -> risk tier, straight from the backend's policy table.
  final Map<String, String> risks;
  final String auditPath;

  int get toolCount => tools.length;

  bool get isEmpty => status.isEmpty && provider.isEmpty && model.isEmpty;

  /// What the status chip calls the brain: the model if we know it, else the
  /// provider, else a plain "local".
  String get label {
    if (model.isNotEmpty) return model;
    if (provider.isNotEmpty) return provider;
    return 'local';
  }

  factory BackendHealth.fromJson(Map<String, dynamic> json) => BackendHealth(
        status: _str(json['status']),
        provider: _str(json['provider']),
        model: _str(json['model']),
        ollamaBaseUrl: _str(json['ollama_base_url']),
        whisperModel: _str(json['whisper_model']),
        ttsVoice: _str(json['tts_voice']),
        wakeEnabled: json['wake_enabled'] is bool
            ? json['wake_enabled'] as bool
            : json['wake_enabled'] != false,
        tools: _list(json['tools']),
        risks: _map(json['tool_risks']),
        auditPath: _str(json['audit_path']),
      );

  static String _str(Object? value) => value is String ? value : '';

  static List<String> _list(Object? value) =>
      value is List ? [for (final item in value) '$item'] : const [];

  static Map<String, String> _map(Object? value) => value is Map
      ? {for (final e in value.entries) '${e.key}': '${e.value}'}
      : const {};
}
