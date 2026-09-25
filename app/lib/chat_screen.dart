import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as status;

import 'config.dart';
import 'health.dart';
import 'hud.dart';
import 'palette.dart';
import 'reconnect.dart';
import 'theme.dart';
import 'transcript.dart';

/// How the microphone is driven.
enum MicMode {
  /// Press and hold the orb button (or the space bar) to talk.
  hold,

  /// Always-listening: the backend watches for the wake word on its own.
  wake,
}

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  // --- transcript & input -------------------------------------------------
  // The mock has no visible composer: the palette is where you type, so it
  // doubles as the message box (`Ask Jarvis: …`) and the command runner.
  late final TextEditingController _paletteInput = TextEditingController();
  final FocusNode _rootFocus = FocusNode(debugLabel: 'root');
  final FocusNode _paletteFocus = FocusNode(debugLabel: 'palette');
  final ScrollController _scroll = ScrollController();
  final Transcript _transcript = Transcript();

  // --- socket -------------------------------------------------------------
  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  bool _connected = false;
  bool _connecting = false;
  final Backoff _backoff = Backoff(jitter: 0.25);
  Timer? _retry;
  Timer? _countdown;
  Duration? _retryIn;

  // --- session flags ------------------------------------------------------
  bool _assistantTyping = false;
  bool _speaking = false;
  bool _confirming = false; // approval dialog is up for a gated tool call
  bool _listening = false; // always-listening (wake word) armed
  String _pttState = 'idle'; // idle | recording | transcribing
  String _wakeWord = 'hey jarvis';
  MicMode _micMode = MicMode.hold;
  bool _spaceHeld = false; // space is acting as push-to-talk right now

  // --- telemetry ----------------------------------------------------------
  BackendHealth _health = BackendHealth.empty;
  final List<int> _latencies = [];
  Stopwatch? _roundTrip;

  /// Loudness for the speaking bars, fed by the reply token stream rather
  /// than a canned animation loop.
  final ValueNotifier<double> _energy = ValueNotifier(0);
  Timer? _decay;

  // --- chrome -------------------------------------------------------------
  bool _transcriptOpen = false;
  bool _followTail = true;
  bool _paletteOpen = false;
  bool _settingsOpen = false;
  String _paletteQuery = '';
  int _paletteIndex = 0;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_onScroll);
    _connect();
  }

  @override
  void dispose() {
    _sub?.cancel();
    _channel?.sink.close(status.normalClosure);
    _retry?.cancel();
    _countdown?.cancel();
    _decay?.cancel();
    _energy.dispose();
    _scroll.removeListener(_onScroll);
    _scroll.dispose();
    _paletteInput.dispose();
    _rootFocus.dispose();
    _paletteFocus.dispose();
    super.dispose();
  }

  // --- connection ---------------------------------------------------------

  void _connect() {
    if (!mounted) return;
    _retry?.cancel();
    _retry = null;
    _countdown?.cancel();
    _countdown = null;
    _sub?.cancel();
    _channel?.sink.close(status.normalClosure);

    setState(() {
      _connecting = true;
      _retryIn = null;
      _connected = false;
    });

    final channel = WebSocketChannel.connect(Uri.parse(kDefaultWsUrl));
    _channel = channel;

    channel.ready.then((_) {
      if (!mounted) return;
      _backoff.reset();
      setState(() {
        _connected = true;
        _connecting = false;
        _retryIn = null;
      });
      _fetchHealth();
    }).catchError((Object _) {
      if (mounted) _scheduleRetry();
    });

    _sub = channel.stream.listen(
      (data) => _handleEvent(data as String),
      onError: (Object _) {
        if (mounted) _scheduleRetry();
      },
      onDone: () {
        if (mounted) _scheduleRetry();
      },
      cancelOnError: false,
    );
  }

  /// Queue the next attempt on the backoff schedule. The backend is a process
  /// you start by hand, so this keeps trying quietly instead of making you
  /// click reconnect after you've started uvicorn.
  void _scheduleRetry() {
    if (!mounted) return;
    setState(() {
      _connected = false;
      _connecting = false;
    });
    if (_retry != null) return;

    final delay = _backoff.next();
    _retry = Timer(delay, () {
      _retry = null;
      _connect();
    });
    _startCountdown(delay);
  }

  void _startCountdown(Duration total) {
    _countdown?.cancel();
    setState(() => _retryIn = total);
    _countdown = Timer.periodic(const Duration(seconds: 1), (timer) {
      final left = (_retryIn ?? Duration.zero) - const Duration(seconds: 1);
      if (!mounted) {
        timer.cancel();
        return;
      }
      if (left <= Duration.zero) {
        timer.cancel();
        setState(() => _retryIn = Duration.zero);
      } else {
        setState(() => _retryIn = left);
      }
    });
  }

  /// One-shot GET /health for the model name, tool list and risk tiers shown
  /// in the settings sheet. Failure is fine — the chip already says offline.
  Future<void> _fetchHealth() async {
    try {
      final ws = Uri.parse(kDefaultWsUrl);
      final uri = ws.replace(
        scheme: ws.scheme == 'wss' ? 'https' : 'http',
        path: '/health',
        query: '',
      );
      final client = HttpClient();
      try {
        final request = await client.getUrl(uri);
        final response = await request.close().timeout(
              const Duration(seconds: 3),
            );
        final body = await response.transform(utf8.decoder).join();
        final map = jsonDecode(body) as Map<String, dynamic>;
        if (!mounted) return;
        setState(() => _health = BackendHealth.fromJson(map));
      } finally {
        client.close(force: true);
      }
    } catch (_) {
      // Health is cosmetic; the socket is what matters.
    }
  }

  void _onScroll() {
    if (!_scroll.hasClients) return;
    final position = _scroll.position;
    _followTail = position.pixels >= position.maxScrollExtent - 120;
  }

  void _scrollToEnd() {
    if (!_followTail) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic,
      );
    });
  }

  // --- protocol -----------------------------------------------------------

  void _handleEvent(String raw) {
    final Object? decoded;
    try {
      decoded = jsonDecode(raw);
    } catch (_) {
      return; // ignore malformed frames
    }
    if (decoded is! Map<String, dynamic>) return;
    final map = decoded; // promote once; closures don't see the is!-promotion

    switch (map['type']) {
      case 'token':
        _roundTrip?.stop();
        final ms = _roundTrip?.elapsedMilliseconds;
        _roundTrip = null;
        setState(() => _transcript.appendAssistantToken((map['text'] ?? '') as String));
        if (ms != null) {
          _latencies.add(ms);
          if (_latencies.length > 12) _latencies.removeAt(0);
        }
        _bumpEnergy();
        _scrollToEnd();
      case 'tool_call':
        setState(() {
          _transcript.startToolCall(
            name: (map['name'] ?? '?') as String,
            arguments:
                (map['arguments'] as Map?)?.cast<String, dynamic>() ?? const {},
            id: map['id'] as String?,
          );
        });
        _scrollToEnd();
      case 'tool_result':
        setState(() => _transcript.finishTool(
              (map['name'] ?? '?') as String,
              map['result'],
            ));
      case 'tool_denied':
        setState(() => _transcript.toolDenied(
              (map['name'] ?? '?') as String,
              status: map['decision'] == 'disabled'
                  ? ToolStatus.disabled
                  : ToolStatus.denied,
              reason: map['reason'] as String?,
              risk: map['risk'] as String?,
            ));
      case 'confirm_request':
        setState(() {
          _transcript.toolAwaitingApproval(
            (map['name'] ?? '?') as String,
            risk: map['risk'] as String?,
            mode: map['mode'] as String?,
          );
          _transcriptOpen = true;
        });
        _askConfirmation(map);
      case 'done':
        setState(() {
          _assistantTyping = false;
          _speaking = false;
          _confirming = false;
          _pttState = 'idle';
        });
      case 'reset_done':
        setState(() {
          _transcript.clear();
          _pttState = 'idle';
        });
      case 'transcript':
        setState(() {
          _transcript.addUser((map['text'] ?? '') as String);
          _assistantTyping = true;
          _transcriptOpen = true;
        });
        _scrollToEnd();
      case 'ptt_state':
        final st = (map['state'] ?? 'idle') as String;
        setState(() {
          _pttState = st;
          if (st == 'transcribing') _assistantTyping = true;
          if (st == 'recording') _assistantTyping = false;
        });
      case 'listen_state':
        final st = (map['state'] ?? 'idle') as String;
        setState(() {
          _listening = st != 'idle';
          if (_listening) {
            _micMode = MicMode.wake;
          } else if (_micMode == MicMode.wake && _pttState == 'idle') {
            _micMode = MicMode.hold;
          }
          if (st == 'capturing') _assistantTyping = true;
        });
      case 'wake_detected':
        setState(() {
          final name = ((map['name'] ?? '') as String).replaceAll('_', ' ');
          if (name.isNotEmpty) _wakeWord = name;
          _assistantTyping = true;
          _transcriptOpen = true;
        });
        _energy.value = 1;
      case 'tts_start':
        setState(() => _speaking = true);
        _energy.value = 1;
      case 'tts_done':
        setState(() => _speaking = false);
      case 'error':
        setState(() {
          _assistantTyping = false;
          // A refused arm shouldn't leave the toggle stuck on "wake".
          if (!_listening) _micMode = MicMode.hold;
        });
        _showError((map['message'] ?? 'error') as String);
      default:
        break;
    }
  }

  /// Every reply token nudges the orb to full loudness; it then decays, so
  /// the bars breathe with the answer as it streams in.
  void _bumpEnergy() {
    _energy.value = 1;
    _decay ??= Timer.periodic(const Duration(milliseconds: 70), (timer) {
      final next = _energy.value * 0.86;
      _energy.value = next < 0.02 ? 0 : next;
      if (_energy.value == 0 && !_assistantTyping) {
        timer.cancel();
        _decay = null;
      }
    });
  }

  void _showError(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        SnackBar(
          content: Text(message),
          duration: const Duration(seconds: 4),
        ),
      );
  }

  // --- sending ------------------------------------------------------------

  void _submit(String raw) {
    final text = raw.trim();
    if (text.isEmpty || !_connected || _assistantTyping) return;
    setState(() {
      _transcript.addUser(text);
      _assistantTyping = true;
      _transcriptOpen = true;
      _followTail = true;
    });
    _roundTrip = Stopwatch()..start();
    _channel!.sink.add(jsonEncode({'type': 'user_message', 'text': text}));
    _scrollToEnd();
  }

  void _sendPtt(String type) {
    if (!_connected) return;
    _channel!.sink.add(jsonEncode({'type': type}));
    if (type == 'ptt_start') {
      setState(() {
        _pttState = 'recording';
        _transcriptOpen = true;
      });
    }
  }

  void _reset() {
    if (_connected) {
      _channel!.sink.add(jsonEncode({'type': 'reset'}));
    } else {
      setState(_transcript.clear);
    }
  }

  void _reconnect() {
    setState(() {
      _transcript.clear();
      _latencies.clear();
    });
    _connect();
  }

  void _setMicMode(MicMode mode) {
    setState(() => _micMode = mode);
    if (!_connected) return;
    if (mode == MicMode.wake) {
      if (!_listening && !_assistantTyping && !_speaking) {
        _channel!.sink.add(jsonEncode({'type': 'listen_start'}));
      }
    } else if (_listening) {
      _channel!.sink.add(jsonEncode({'type': 'listen_stop'}));
    }
  }

  void _toggleListening() {
    if (!_connected) return;
    if (_listening) {
      _channel!.sink.add(jsonEncode({'type': 'listen_stop'}));
    } else {
      if (_assistantTyping || _speaking) return;
      _channel!.sink.add(jsonEncode({'type': 'listen_start'}));
    }
  }

  /// Ask the human to approve a gated tool call, then answer the backend.
  ///
  /// The backend enforces this too — a typed tier is only satisfied by an
  /// exact echo of its challenge phrase — so matching the text here just
  /// keeps the Allow button honest, it isn't the security boundary.
  Future<void> _askConfirmation(Map<String, dynamic> request) async {
    if (!mounted) return;
    final name = (request['name'] ?? 'action') as String;
    final risk = (request['risk'] ?? 'unknown') as String;
    final mode = (request['mode'] ?? 'tap') as String;
    final challenge = (request['challenge'] ?? '') as String;
    final args = jsonEncode(request['arguments'] ?? const <String, dynamic>{});
    final typed = TextEditingController();
    setState(() => _confirming = true);

    bool ok = false;
    String typedText = '';
    try {
      final approved = await showDialog<bool>(
        context: context,
        barrierDismissible: false,
        barrierColor: Colors.black.withValues(alpha: 0.6),
        builder: (ctx) => StatefulBuilder(
          builder: (ctx, setDialogState) {
            final matches = typed.text.trim() == challenge;
            final danger = mode == 'typed' || risk == 'destructive';
            final accent = danger ? Hud.amber : Hud.violet;
            return Dialog(
              backgroundColor: Colors.transparent,
              elevation: 0,
              insetPadding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Glass(
                  radius: 20,
                  fill: Hud.sheet,
                  blur: 26,
                  borderColor: danger
                      ? Hud.amber.withValues(alpha: 0.45)
                      : Hud.borderStrong,
                  padding: const EdgeInsets.all(20),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(
                            mode == 'typed'
                                ? Icons.warning_amber_rounded
                                : Icons.shield_outlined,
                            size: 18,
                            color: accent,
                          ),
                          const SizedBox(width: 10),
                          Text(
                            'Allow this action?',
                            style: hudUi(size: 16.5, weight: FontWeight.w600),
                          ),
                        ],
                      ),
                      const SizedBox(height: 14),
                      Container(
                        width: double.infinity,
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: Hud.bg.withValues(alpha: 0.55),
                          borderRadius: BorderRadius.circular(10),
                          border: Border.all(color: Hud.border),
                        ),
                        child: SelectableText(
                          '$name($args)',
                          style: hudMono(size: 11.5, color: Hud.cyan, height: 1.5),
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        'risk tier · $risk',
                        style: hudMono(
                          size: 10.5,
                          color: danger ? Hud.amber : Hud.textDim,
                          letterSpacing: 0.4,
                        ),
                      ),
                      if (mode == 'typed') ...[
                        const SizedBox(height: 14),
                        Text(
                          'Type "$challenge" to confirm',
                          style: hudMono(size: 11, color: Hud.textDim),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: typed,
                          autofocus: true,
                          onChanged: (_) => setDialogState(() {}),
                          style: hudMono(size: 13),
                          decoration: InputDecoration(
                            isDense: true,
                            filled: true,
                            fillColor: Hud.bg.withValues(alpha: 0.6),
                            hintText: challenge,
                            hintStyle: hudMono(size: 13, color: Hud.textFaint),
                            contentPadding: const EdgeInsets.symmetric(
                              horizontal: 12,
                              vertical: 12,
                            ),
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(10),
                              borderSide: const BorderSide(color: Hud.border),
                            ),
                            enabledBorder: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(10),
                              borderSide: const BorderSide(color: Hud.border),
                            ),
                            focusedBorder: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(10),
                              borderSide: BorderSide(color: accent),
                            ),
                          ),
                        ),
                      ],
                      const SizedBox(height: 18),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.end,
                        children: [
                          _TextButton(
                            label: 'Deny',
                            onTap: () => Navigator.of(ctx).pop(false),
                          ),
                          const SizedBox(width: 8),
                          _GradientButton(
                            label: 'Allow',
                            enabled: !(mode == 'typed' && !matches),
                            onTap: () => Navigator.of(ctx).pop(true),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            );
          },
        ),
      );
      ok = approved ?? false;
      typedText = typed.text.trim();
    } finally {
      typed.dispose();
      if (mounted) setState(() => _confirming = false);
    }

    _channel?.sink.add(jsonEncode({
      'type': 'confirm_response',
      'approved': ok,
      'challenge': mode == 'typed' ? typedText : null,
    }));
  }

  // --- derived state ------------------------------------------------------

  /// The orb's current state, and what the caption under it should say.
  ({OrbState orb, String label, Color labelColor, String caption}) get _view {
    if (!_connected) {
      final left = _retryIn;
      return (
        orb: OrbState.offline,
        label: _connecting ? 'connecting' : 'offline',
        labelColor: Hud.amber,
        caption: left != null && left > Duration.zero
            ? 'Backend offline — retrying in ${math.max(1, left.inSeconds)}s'
            : 'Backend offline — start it, I\'ll connect on my own',
      );
    }
    if (_confirming) {
      return (
        orb: OrbState.thinking,
        label: 'confirming',
        labelColor: Hud.violet,
        caption: 'Waiting for your approval…',
      );
    }
    if (_pttState == 'recording') {
      return (
        orb: OrbState.listening,
        label: 'listening',
        labelColor: Hud.cyan,
        caption: 'Go ahead, I\'m listening…',
      );
    }
    if (_pttState == 'transcribing') {
      return (
        orb: OrbState.thinking,
        label: 'thinking',
        labelColor: Hud.violet,
        caption: 'Transcribing…',
      );
    }
    if (_assistantTyping) {
      return (
        orb: OrbState.thinking,
        label: 'thinking',
        labelColor: Hud.violet,
        caption: 'Working on it…',
      );
    }
    if (_speaking) {
      final last = _lastAssistantText();
      return (
        orb: OrbState.speaking,
        label: 'speaking',
        labelColor: Hud.cyan,
        caption: last ?? 'Speaking…',
      );
    }
    if (_listening) {
      return (
        orb: OrbState.listening,
        label: 'listening',
        labelColor: Hud.cyan,
        caption: 'Say "$_wakeWord" whenever you\'re ready',
      );
    }
    return (
      orb: OrbState.idle,
      label: 'idle',
      labelColor: Hud.textFaint,
      caption: 'Say "Jarvis" or tap the mic to start',
    );
  }

  String? _lastAssistantText() {
    for (final entry in _transcript.entries.reversed) {
      if (entry is AssistantEntry && entry.text.trim().isNotEmpty) {
        final text = entry.text.trim();
        return text.length <= 170 ? text : '${text.substring(0, 170)}…';
      }
    }
    return null;
  }

  // --- keyboard -----------------------------------------------------------

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is KeyRepeatEvent) return KeyEventResult.ignored;
    final ctrl = HardwareKeyboard.instance.isControlPressed ||
        HardwareKeyboard.instance.isMetaPressed;

    if (event is KeyDownEvent) {
      if (ctrl && event.logicalKey == LogicalKeyboardKey.keyK) {
        _openPalette();
        return KeyEventResult.handled;
      }
      if (ctrl && event.logicalKey == LogicalKeyboardKey.comma) {
        _openSettings();
        return KeyEventResult.handled;
      }
      if (event.logicalKey == LogicalKeyboardKey.escape) {
        if (_paletteOpen) {
          _closeOverlays();
          return KeyEventResult.handled;
        }
        if (_settingsOpen) {
          _closeOverlays();
          return KeyEventResult.handled;
        }
        if (_transcriptOpen) {
          setState(() => _transcriptOpen = false);
          return KeyEventResult.handled;
        }
      }
      // Space is push-to-talk whenever you aren't typing in a field.
      if (event.logicalKey == LogicalKeyboardKey.space &&
          !ctrl &&
          !_paletteOpen &&
          !_settingsOpen &&
          !_spaceHeld) {
        _spaceHeld = true;
        _sendPtt('ptt_start');
        return KeyEventResult.handled;
      }
      // Typing anywhere opens the composer with that first keystroke, so the
      // shell stays keyboard-first even though the mock has no input field.
      final typed = event.character ?? _printableLabel(event.logicalKey);
      if (!ctrl &&
          !HardwareKeyboard.instance.isAltPressed &&
          !_paletteOpen &&
          !_settingsOpen &&
          !_confirming &&
          typed != null &&
          typed.trim().isNotEmpty &&
          !_isModifierKey(event.logicalKey)) {
        _openPalette(seed: typed);
        return KeyEventResult.handled;
      }
    }

    if (event is KeyUpEvent &&
        event.logicalKey == LogicalKeyboardKey.space &&
        _spaceHeld) {
      _spaceHeld = false;
      _sendPtt('ptt_stop');
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  void _openPalette({String seed = ''}) {
    _paletteInput.text = seed;
    _paletteInput.selection = TextSelection.collapsed(offset: seed.length);
    setState(() {
      _paletteOpen = true;
      _settingsOpen = false;
      _paletteQuery = seed;
      _paletteIndex = 0;
    });
    WidgetsBinding.instance
        .addPostFrameCallback((_) => _paletteFocus.requestFocus());
  }

  /// `KeyEvent.character` is the right source for typed text, but it isn't
  /// always populated; for a single printable key the label is just as good.
  static String? _printableLabel(LogicalKeyboardKey key) {
    final label = key.keyLabel;
    return RegExp(r'^[A-Za-z0-9]$').hasMatch(label)
        ? label.toLowerCase()
        : null;
  }

  static bool _isModifierKey(LogicalKeyboardKey key) =>
      key == LogicalKeyboardKey.shiftLeft ||
      key == LogicalKeyboardKey.shiftRight ||
      key == LogicalKeyboardKey.controlLeft ||
      key == LogicalKeyboardKey.controlRight ||
      key == LogicalKeyboardKey.altLeft ||
      key == LogicalKeyboardKey.altRight ||
      key == LogicalKeyboardKey.metaLeft ||
      key == LogicalKeyboardKey.metaRight ||
      key == LogicalKeyboardKey.capsLock;

  void _openSettings() {
    setState(() {
      _settingsOpen = true;
      _paletteOpen = false;
    });
    _fetchHealth();
  }

  void _closeOverlays() {
    setState(() {
      _paletteOpen = false;
      _settingsOpen = false;
    });
    _rootFocus.requestFocus();
  }

  // --- command palette ----------------------------------------------------

  /// Commands, plus a few suggested openers while the query is empty — once
  /// you're typing, the `Ask Jarvis` row takes over from the canned prompts.
  List<PaletteAction> _paletteActions() => [
        const PaletteAction(
          id: 'reset',
          label: 'New session',
          detail: 'Forget the conversation and start over',
          keywords: ['clear', 'restart', 'forget'],
        ),
        const PaletteAction(
          id: 'reconnect',
          label: 'Reconnect to backend',
          detail: 'Close the socket and dial $kDefaultWsUrl again',
          keywords: ['socket', 'ws', 'backend', 'retry'],
        ),
        PaletteAction(
          id: 'wake',
          label: _listening ? 'Stop always-listening' : 'Always-listen for wake word',
          detail: _listening
              ? 'Disarm the wake word and go back to hold-to-talk'
              : 'Arm the mic and wait for "$_wakeWord"',
          keywords: ['wake', 'voice', 'mic', 'armed', 'listen'],
        ),
        const PaletteAction(
          id: 'transcript',
          label: 'Toggle transcript',
          detail: 'Show or hide the conversation drawer',
          keywords: ['history', 'log', 'messages'],
        ),
        const PaletteAction(
          id: 'settings',
          label: 'Settings & backend info',
          detail: 'Model, speech pipeline, risk tiers for every tool',
          keywords: ['config', 'health', 'tools', 'model'],
        ),
        if (_paletteQuery.trim().isEmpty) ...const [
          PaletteAction(
            id: 'p_stats',
            label: 'What are my system stats?',
            detail: 'What are my system stats?',
            keywords: ['cpu', 'gpu', 'ram', 'system'],
            prompt: true,
          ),
          PaletteAction(
            id: 'p_tests',
            label: 'Find the test files',
            detail: 'Find the test files',
            keywords: ['search', 'pytest', 'flutter test'],
            prompt: true,
          ),
          PaletteAction(
            id: 'p_notepad',
            label: 'Open notepad',
            detail: 'Open notepad',
            keywords: ['app', 'launch', 'confirm'],
            prompt: true,
          ),
        ],
      ];

  /// While the palette is empty it's a command list; as soon as you type,
  /// it's a message box. The `Ask Jarvis` row goes first so Enter always
  /// sends what you typed — and so it's never below the fold — with the
  /// matching commands one arrow-key away underneath.
  List<PaletteAction> _paletteResults() {
    final text = _paletteQuery.trim();
    if (text.isEmpty) return rankActions(_paletteActions(), text);
    return [
      PaletteAction(
        id: 'ask',
        label: 'Ask Jarvis',
        detail: text,
        prompt: true,
      ),
      ...rankActions(_paletteActions(), text),
    ];
  }

  void _moveSelection(int delta) {
    final results = _paletteResults();
    if (results.isEmpty) return;
    setState(() {
      _paletteIndex = (_paletteIndex + delta) % results.length;
      if (_paletteIndex < 0) _paletteIndex += results.length;
    });
  }

  void _runSelected() {
    if (!_paletteOpen) return;
    final results = _paletteResults();
    if (results.isEmpty) return;
    _runAction(results[_paletteIndex.clamp(0, results.length - 1)]);
  }

  void _runAction(PaletteAction action) {
    _closeOverlays();
    switch (action.id) {
      case 'reset':
        _reset();
      case 'reconnect':
        _reconnect();
      case 'wake':
        _toggleListening();
      case 'transcript':
        setState(() => _transcriptOpen = !_transcriptOpen);
      case 'settings':
        _openSettings();
      default:
        if (!action.prompt) return;
        if (!_connected) {
          _showError('Backend offline — that message wasn\'t sent');
          return;
        }
        _submit(action.detail);
    }
  }

  // --- build --------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.sizeOf(context);
    final view = _view;
    final busy = _assistantTyping || _speaking || _confirming;
    final panelMax = (size.height * 0.30).clamp(120.0, 320.0);
    final bubbleMax = (size.width * 0.8).clamp(220.0, 520.0);

    return Scaffold(
      body: Focus(
        focusNode: _rootFocus,
        autofocus: true,
        onKeyEvent: _onKey,
        child: Stack(
          fit: StackFit.expand,
          children: [
            const HudBackdrop(),
            SafeArea(
              child: Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 600),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 18),
                    child: Column(
                      children: [
                        _statusBar(),
                        Expanded(child: _stage(view, size)),
                        _transcriptToggle(),
                        _transcriptPanel(panelMax, bubbleMax),
                        _dock(view, busy, size),
                      ],
                    ),
                  ),
                ),
              ),
            ),
            if (_paletteOpen) _buildPalette(),
            if (_settingsOpen) _buildSettings(size),
          ],
        ),
      ),
    );
  }

  // --- status bar ---------------------------------------------------------

  Widget _statusBar() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(6, 22, 6, 8),
      child: Row(
        children: [
          Container(
            width: 22,
            height: 22,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(Hud.rChip),
              gradient: Hud.brand,
            ),
          ),
          const SizedBox(width: 9),
          Text(
            'Jarvis',
            style: hudUi(size: 15.5, weight: FontWeight.w600, letterSpacing: 0.2),
          ),
          const Spacer(),
          if (_latencies.isNotEmpty) ...[
            _LatencyChip(samples: _latencies, onTap: _openSettings),
            const SizedBox(width: 8),
          ],
          _modeChip(),
          const SizedBox(width: 8),
          _iconButton(
            icon: Icons.settings_outlined,
            tooltip: 'Settings · Ctrl+,',
            onTap: _openSettings,
          ),
        ],
      ),
    );
  }

  Widget _modeChip() {
    final online = _connected;
    final text = online
        ? 'online · ${_health.label}'
        : _connecting
            ? 'connecting…'
            : 'offline';
    return Pressable(
      onTap: online ? _openSettings : _connect,
      builder: (context, hovered) => Glass(
        radius: Hud.rPill,
        blur: 16,
        fill: hovered ? Hud.glassStrong : Hud.glass,
        borderColor: hovered ? Hud.borderStrong : Hud.border,
        padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 8),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 6,
              height: 6,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: online ? Hud.cyan : Hud.textFaint,
                boxShadow: online
                    ? [const BoxShadow(color: Hud.cyan, blurRadius: 8)]
                    : null,
              ),
            ),
            const SizedBox(width: 7),
            Text(
              text,
              style: hudMono(
                size: 10.5,
                letterSpacing: 0.3,
                color: hovered ? Hud.text : Hud.textDim,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _iconButton({
    required IconData icon,
    required String tooltip,
    VoidCallback? onTap,
  }) {
    return Tooltip(
      message: tooltip,
      child: Pressable(
        onTap: onTap,
        builder: (context, hovered) => AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          width: 34,
          height: 34,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: hovered ? Hud.glassStrong : Hud.glass,
            border: Border.all(
              color: hovered ? Hud.borderStrong : Hud.border,
            ),
          ),
          child: Icon(
            icon,
            size: 15,
            color: hovered ? Hud.text : Hud.textDim,
          ),
        ),
      ),
    );
  }

  // --- stage --------------------------------------------------------------

  Widget _stage(
    ({OrbState orb, String label, Color labelColor, String caption}) view,
    Size size,
  ) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final orbSize = (constraints.maxHeight * 0.46).clamp(140.0, 208.0);
        return SingleChildScrollView(
          physics: const ClampingScrollPhysics(),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: constraints.maxHeight),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Orb(state: view.orb, size: orbSize, energy: _energy),
                SizedBox(height: orbSize > 190 ? 30 : 20),
                ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 380),
                  child: Column(
                    children: [
                      Text(
                        view.label.toUpperCase(),
                        style: hudMono(
                          size: 10.5,
                          letterSpacing: 1.6,
                          color: view.labelColor,
                        ),
                      ),
                      const SizedBox(height: 10),
                      Text(
                        view.caption,
                        textAlign: TextAlign.center,
                        style: hudUi(
                          size: 16,
                          height: 1.55,
                          color: view.labelColor == Hud.textFaint
                              ? Hud.textDim
                              : Hud.text,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  // --- transcript ---------------------------------------------------------

  Widget _transcriptToggle() {
    return Pressable(
      onTap: () {
        setState(() => _transcriptOpen = !_transcriptOpen);
        if (_transcriptOpen) {
          _followTail = true;
          _scrollToEnd();
        }
      },
      builder: (context, hovered) => Glass(
        radius: Hud.rPill,
        blur: 16,
        fill: hovered ? Hud.glassStrong : Hud.glass,
        borderColor: hovered ? Hud.borderStrong : Hud.border,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              'Transcript',
              style: hudMono(
                size: 10,
                letterSpacing: 0.6,
                color: hovered ? Hud.textDim : Hud.textFaint,
              ),
            ),
            if (!_transcript.isEmpty) ...[
              const SizedBox(width: 7),
              Text(
                '${_transcript.length}',
                style: hudMono(size: 10, color: Hud.cyan),
              ),
            ],
            const SizedBox(width: 6),
            AnimatedRotation(
              turns: _transcriptOpen ? 0.5 : 0,
              duration: const Duration(milliseconds: 200),
              child: Icon(
                Icons.keyboard_arrow_down_rounded,
                size: 13,
                color: hovered ? Hud.textDim : Hud.textFaint,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _transcriptPanel(double panelMax, double bubbleMax) {
    return AnimatedSize(
      duration: const Duration(milliseconds: 260),
      curve: Curves.easeOutCubic,
      alignment: Alignment.topCenter,
      child: !_transcriptOpen
          ? const SizedBox(width: double.infinity)
          : Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Glass(
                radius: Hud.rPanel,
                blur: 20,
                child: ConstrainedBox(
                  constraints: BoxConstraints(maxHeight: panelMax),
                  child: _transcript.isEmpty
                      ? Padding(
                          padding: const EdgeInsets.all(20),
                          child: Text(
                            'No conversation yet — type below, hold the mic, '
                            'or hit Ctrl+K for commands.',
                            textAlign: TextAlign.center,
                            style: hudMono(size: 11.5, color: Hud.textFaint),
                          ),
                        )
                      : ListView.separated(
                          controller: _scroll,
                          padding: const EdgeInsets.fromLTRB(18, 16, 18, 16),
                          itemCount: _transcript.length,
                          separatorBuilder: (_, __) => const SizedBox(height: 12),
                          itemBuilder: (context, i) {
                            final entry = _transcript.entries[i];
                            return _Enter(
                              key: ObjectKey(entry),
                              child: switch (entry) {
                                UserEntry e => _Bubble(
                                    text: e.text,
                                    isUser: true,
                                    maxWidth: bubbleMax,
                                  ),
                                AssistantEntry e => _Bubble(
                                    text: e.text,
                                    isUser: false,
                                    maxWidth: bubbleMax,
                                  ),
                                ToolEntry e => _ToolRow(entry: e),
                              },
                            );
                          },
                        ),
                ),
              ),
            ),
    );
  }

  // --- dock ---------------------------------------------------------------

  Widget _dock(
    ({OrbState orb, String label, Color labelColor, String caption}) view,
    bool busy,
    Size size,
  ) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(6, 16, 6, 22),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _MicButton(
            recording: _pttState == 'recording',
            armed: _listening,
            enabled: _connected && !busy,
            mode: _micMode,
            onDown: () => _sendPtt('ptt_start'),
            onUp: () => _sendPtt('ptt_stop'),
            onTap: _toggleListening,
          ),
          const SizedBox(height: 16),
          _segmented(),
          const SizedBox(height: 16),
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 320),
            child: Text(
              _hint(view),
              textAlign: TextAlign.center,
              style: hudUi(size: 10.5, height: 1.6, color: Hud.textFaint),
            ),
          ),
        ],
      ),
    );
  }

  String _hint(
    ({OrbState orb, String label, Color labelColor, String caption}) view,
  ) {
    if (!_connected) {
      return 'Backend offline — start it and the socket will come back on its own.';
    }
    if (_listening) {
      return 'Always-listening is armed — just say "$_wakeWord".';
    }
    if (view.orb == OrbState.speaking) {
      return 'Speaking the reply aloud. Tap the mic to interrupt.';
    }
    if (view.orb == OrbState.thinking || view.orb == OrbState.listening) {
      return 'Working — press Esc to collapse the transcript.';
    }
    return 'Hold the mic (or the space bar) to talk · just start typing to '
        'send a message · Ctrl+K for commands';
  }

  Widget _segmented() {
    return Glass(
      radius: Hud.rPill,
      blur: 16,
      padding: const EdgeInsets.all(4),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const SizedBox(width: 2),
          _segment('Hold', MicMode.hold),
          const SizedBox(width: 2),
          _segment('Wake', MicMode.wake),
          const SizedBox(width: 2),
        ],
      ),
    );
  }

  Widget _segment(String label, MicMode mode) {
    final selected = _micMode == mode;
    return Pressable(
      onTap: () => _setMicMode(mode),
      builder: (context, hovered) => AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(Hud.rPill),
          color: selected ? Hud.text : Colors.transparent,
        ),
        child: Text(
          label.toUpperCase(),
          style: hudMono(
            size: 10,
            letterSpacing: 0.4,
            color: selected
                ? Hud.bg
                : hovered
                    ? Hud.textDim
                    : Hud.textFaint,
          ),
        ),
      ),
    );
  }

  // --- overlays -----------------------------------------------------------

  Widget _buildPalette() {
    final results = _paletteResults();
    final index = results.isEmpty ? 0 : _paletteIndex.clamp(0, results.length - 1);

    return _Overlay(
      onDismiss: _closeOverlays,
      child: Glass(
        radius: 20,
        fill: Hud.sheet,
        blur: 26,
        borderColor: Hud.borderStrong,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 12, 12),
              child: Row(
                children: [
                  const Icon(Icons.search_rounded, size: 16, color: Hud.textFaint),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Shortcuts(
                      shortcuts: const {
                        SingleActivator(LogicalKeyboardKey.arrowDown):
                            _PaletteMove(1),
                        SingleActivator(LogicalKeyboardKey.arrowUp):
                            _PaletteMove(-1),
                        SingleActivator(LogicalKeyboardKey.enter): _PaletteRun(),
                      },
                      child: Actions(
                        actions: <Type, Action<Intent>>{
                          _PaletteMove: CallbackAction<_PaletteMove>(
                            onInvoke: (intent) {
                              _moveSelection(intent.delta);
                              return null;
                            },
                          ),
                          _PaletteRun: CallbackAction<_PaletteRun>(
                            onInvoke: (_) {
                              _runSelected();
                              return null;
                            },
                          ),
                        },
                        child: TextField(
                          controller: _paletteInput,
                          focusNode: _paletteFocus,
                          autofocus: true,
                          style: hudUi(size: 15),
                          onChanged: (value) => setState(() {
                            _paletteQuery = value;
                            _paletteIndex = 0;
                          }),
                          onSubmitted: (_) => _runSelected(),
                          decoration: const InputDecoration(
                            hintText: 'Ask anything, or type a command…',
                          ),
                        ),
                      ),
                    ),
                  ),
                  const _Keycap('esc'),
                ],
              ),
            ),
            const Divider(height: 1, color: Hud.border),
            ConstrainedBox(
              constraints: const BoxConstraints(maxHeight: 320),
              child:                results.isEmpty
                  ? Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(
                        'Nothing to run yet — type a command.',
                        style: hudMono(size: 11.5, color: Hud.textFaint),
                      ),
                    )
                  : ListView.builder(
                      shrinkWrap: true,
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      itemCount: results.length,
                      itemBuilder: (context, i) => _PaletteRow(
                        action: results[i],
                        selected: i == index,
                        onTap: () => _runAction(results[i]),
                        onHover: () => setState(() => _paletteIndex = i),
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSettings(Size size) {
    final rows = <(String, String)>[
      ('socket', kDefaultWsUrl),
      ('status', _connected ? 'connected' : _connecting ? 'connecting' : 'offline'),
      ('provider', _health.provider.isEmpty ? '—' : _health.provider),
      ('model', _health.model.isEmpty ? '—' : _health.model),
      ('endpoint', _health.ollamaBaseUrl.isEmpty ? '—' : _health.ollamaBaseUrl),
      ('audit log', _health.auditPath.isEmpty ? '—' : _health.auditPath),
    ];
    final speech = <(String, String)>[
      ('stt', _health.whisperModel.isEmpty ? '—' : _health.whisperModel),
      ('tts voice', _health.ttsVoice.isEmpty ? '—' : _health.ttsVoice),
      ('wake word', '$_wakeWord (${_health.wakeEnabled ? 'enabled' : 'disabled'})'),
      ('input', _micMode == MicMode.wake ? 'always-listening' : 'push-to-talk'),
    ];

    return _Overlay(
      onDismiss: _closeOverlays,
      child: Glass(
        radius: 20,
        fill: Hud.sheet,
        blur: 26,
        borderColor: Hud.borderStrong,
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: size.height * 0.76),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 16, 14, 14),
                child: Row(
                  children: [
                    Text(
                      'Settings',
                      style: hudUi(size: 16.5, weight: FontWeight.w600),
                    ),
                    const Spacer(),
                    Pressable(
                      onTap: _closeOverlays,
                      builder: (context, hovered) => Icon(
                        Icons.close_rounded,
                        size: 17,
                        color: hovered ? Hud.text : Hud.textFaint,
                      ),
                    ),
                  ],
                ),
              ),
              const Divider(height: 1, color: Hud.border),
              Flexible(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _section('connection', rows),
                      const SizedBox(height: 18),
                      _section('speech pipeline', speech),
                      const SizedBox(height: 18),
                      _tools(),
                    ],
                  ),
                ),
              ),
              const Divider(height: 1, color: Hud.border),
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 14),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    _TextButton(
                      label: 'New session',
                      onTap: () {
                        _closeOverlays();
                        _reset();
                      },
                    ),
                    const SizedBox(width: 8),
                    _GradientButton(
                      label: _connected ? 'Reconnect' : 'Connect now',
                      onTap: () {
                        _closeOverlays();
                        _reconnect();
                      },
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _section(String title, List<(String, String)> rows) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title.toUpperCase(),
          style: hudMono(size: 9.5, letterSpacing: 1.6, color: Hud.textFaint),
        ),
        const SizedBox(height: 10),
        for (final (label, value) in rows)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 78,
                  child: Text(
                    label,
                    style: hudMono(size: 10.5, color: Hud.textFaint),
                  ),
                ),
                Expanded(
                  child: SelectableText(
                    value,
                    style: hudMono(size: 11.5, color: Hud.text, height: 1.45),
                  ),
                ),
              ],
            ),
          ),
      ],
    );
  }

  Widget _tools() {
    if (_health.tools.isEmpty) {
      return Text(
        'TOOLS\n\nNo tool list yet — the backend answers /health with one.',
        style: hudMono(size: 11.5, color: Hud.textFaint, height: 1.6),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'TOOLS · ${_health.toolCount} allow-listed',
          style: hudMono(size: 9.5, letterSpacing: 1.6, color: Hud.textFaint),
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final name in _health.tools)
              _RiskPill(name: name, risk: _health.risks[name] ?? 'unknown'),
          ],
        ),
      ],
    );
  }
}

// --- shared chrome ---------------------------------------------------------

/// Barrier + centred panel with a short entrance, used by both overlays.
class _Overlay extends StatelessWidget {
  const _Overlay({required this.child, required this.onDismiss});

  final Widget child;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.expand,
      children: [
        GestureDetector(
          onTap: onDismiss,
          child: ColoredBox(color: Colors.black.withValues(alpha: 0.5)),
        ),
        Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: TweenAnimationBuilder<double>(
              tween: Tween(begin: 0, end: 1),
              duration: const Duration(milliseconds: 160),
              curve: Curves.easeOutCubic,
              builder: (context, t, inner) => Opacity(
                opacity: t,
                child: Transform.scale(scale: 0.97 + 0.03 * t, child: inner),
              ),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 460),
                child: child,
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class _Keycap extends StatelessWidget {
  const _Keycap(this.label);

  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(6),
          color: Hud.glass,
          border: Border.all(color: Hud.border),
        ),
        child: Text(
          label,
          style: hudMono(size: 9, letterSpacing: 0.5, color: Hud.textFaint),
        ),
      ),
    );
  }
}

class _TextButton extends StatelessWidget {
  const _TextButton({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Pressable(
      onTap: onTap,
      builder: (context, hovered) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
        child: Text(
          label,
          style: hudUi(
            size: 13,
            weight: FontWeight.w500,
            color: hovered ? Hud.text : Hud.textDim,
          ),
        ),
      ),
    );
  }
}

class _GradientButton extends StatelessWidget {
  const _GradientButton({
    required this.label,
    required this.onTap,
    this.enabled = true,
  });

  final String label;
  final VoidCallback onTap;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    return Pressable(
      onTap: enabled ? onTap : null,
      enabled: enabled,
      builder: (context, hovered) => Opacity(
        opacity: enabled ? 1 : 0.4,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Hud.rPill),
            gradient: Hud.brand,
            boxShadow: hovered && enabled
                ? [
                    BoxShadow(
                      color: Hud.cyan.withValues(alpha: 0.28),
                      blurRadius: 22,
                    ),
                  ]
                : null,
          ),
          child: Text(
            label,
            style: hudUi(
              size: 13,
              weight: FontWeight.w600,
              color: Hud.bg,
            ),
          ),
        ),
      ),
    );
  }
}

/// Reply-latency sparkline: a chip-sized readout of the last dozen replies.
class _LatencyChip extends StatelessWidget {
  const _LatencyChip({required this.samples, this.onTap});

  final List<int> samples;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final sorted = [...samples]..sort();
    final median = sorted[sorted.length ~/ 2];
    return Tooltip(
      message: 'last reply ${samples.last} ms · median $median ms',
      child: Pressable(
        onTap: onTap,
        builder: (context, hovered) => Glass(
          radius: Hud.rPill,
          blur: 16,
          fill: hovered ? Hud.glassStrong : Hud.glass,
          borderColor: hovered ? Hud.borderStrong : Hud.border,
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          child: SizedBox(
            width: 44,
            height: 16,
            child: CustomPaint(painter: _SparkPainter(samples)),
          ),
        ),
      ),
    );
  }
}

class _SparkPainter extends CustomPainter {
  const _SparkPainter(this.samples);

  final List<int> samples;

  @override
  void paint(Canvas canvas, Size size) {
    if (samples.isEmpty) return;
    final peak = math.max(samples.reduce(math.max), 1);
    final gap = 1.5;
    final barWidth = (size.width - gap * (samples.length - 1)) / samples.length;
    final paint = Paint()..color = Hud.cyan.withValues(alpha: 0.85);

    for (var i = 0; i < samples.length; i++) {
      final t = (samples[i] / peak).clamp(0.12, 1.0);
      final h = size.height * t;
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(i * (barWidth + gap), size.height - h, barWidth, h),
          const Radius.circular(1.5),
        ),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _SparkPainter old) =>
      old.samples.length != samples.length ||
      (old.samples.isNotEmpty &&
          samples.isNotEmpty &&
          old.samples.last != samples.last);
}

// --- dock pieces -----------------------------------------------------------

/// The hero mic: 60px circle, cyan→violet while live, pulsing ring.
class _MicButton extends StatefulWidget {
  const _MicButton({
    required this.recording,
    required this.armed,
    required this.enabled,
    required this.mode,
    required this.onTap,
    this.onDown,
    this.onUp,
  });

  final bool recording;
  final bool armed;
  final bool enabled;
  final MicMode mode;
  final VoidCallback onTap;
  final VoidCallback? onDown;
  final VoidCallback? onUp;

  @override
  State<_MicButton> createState() => _MicButtonState();
}

class _MicButtonState extends State<_MicButton>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1700),
  );
  bool _hovered = false;

  @override
  void initState() {
    super.initState();
    if (_live) _pulse.repeat();
  }

  bool get _live => widget.recording || widget.armed;

  @override
  void didUpdateWidget(covariant _MicButton old) {
    super.didUpdateWidget(old);
    if (_live != (old.recording || old.armed)) {
      _live ? _pulse.repeat() : _pulse.stop();
      if (!_live) _pulse.value = 0;
    }
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final live = _live;
    final hold = widget.mode == MicMode.hold;
    final tooltip = live
        ? (widget.recording ? 'Release to send' : 'Tap to stop listening')
        : hold
            ? 'Hold to talk'
            : 'Tap to start listening';

    return Tooltip(
      message: tooltip,
      child: MouseRegion(
        cursor: widget.enabled
            ? SystemMouseCursors.click
            : SystemMouseCursors.basic,
        onEnter: (_) => setState(() => _hovered = true),
        onExit: (_) => setState(() => _hovered = false),
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTapDown: widget.enabled && hold ? (_) => widget.onDown?.call() : null,
          onTapUp: widget.enabled && hold ? (_) => widget.onUp?.call() : null,
          onTapCancel: widget.enabled && hold ? () => widget.onUp?.call() : null,
          onTap: widget.enabled && !hold ? widget.onTap : null,
          child: AnimatedScale(
            scale: _hovered && widget.enabled ? 1.045 : 1,
            duration: const Duration(milliseconds: 140),
            child: AnimatedBuilder(
              animation: _pulse,
              builder: (context, child) => Stack(
                alignment: Alignment.center,
                children: [
                  if (live)
                    Transform.scale(
                      scale: 0.9 + 0.45 * _pulse.value,
                      child: Container(
                        width: 60,
                        height: 60,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          border: Border.all(
                            color: Hud.cyan.withValues(
                              alpha: 0.55 * (1 - _pulse.value),
                            ),
                          ),
                        ),
                      ),
                    ),
                  child!,
                ],
              ),
              child: Glass(
                radius: Hud.rPill,
                blur: 16,
                fill: live ? Colors.transparent : Hud.glassStrong,
                borderColor: live
                    ? Colors.transparent
                    : _hovered && widget.enabled
                        ? Hud.cyan
                        : Hud.borderStrong,
                child: Container(
                  width: 60,
                  height: 60,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: live ? Hud.brand : null,
                  ),
                  child: Icon(
                    widget.recording
                        ? Icons.stop_rounded
                        : widget.armed
                            ? Icons.graphic_eq_rounded
                            : Icons.mic_none_rounded,
                    size: 21,
                    color: live
                        ? Hud.bg
                        : widget.enabled
                            ? Hud.text
                            : Hud.textFaint,
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// --- transcript pieces -----------------------------------------------------

/// One message row — the mock's rounded glass bubbles.
class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.text,
    required this.isUser,
    required this.maxWidth,
  });

  final String text;
  final bool isUser;
  final double maxWidth;

  @override
  Widget build(BuildContext context) {
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: maxWidth),
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: isUser ? null : Hud.glass,
            gradient: isUser ? Hud.outgoing : null,
            border: Border.all(
              color: isUser ? Hud.borderStrong : Hud.border,
            ),
            borderRadius: BorderRadius.only(
              topLeft: const Radius.circular(Hud.rBubble),
              topRight: const Radius.circular(Hud.rBubble),
              bottomLeft: Radius.circular(isUser ? Hud.rBubble : 4),
              bottomRight: Radius.circular(isUser ? 4 : Hud.rBubble),
            ),
          ),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 9),
            child: SelectableText(
              text,
              style: hudUi(size: 13, height: 1.5),
            ),
          ),
        ),
      ),
    );
  }
}

/// One tool call as a compact row: name, arguments, status. Tap to see the
/// result. Declined and disabled calls are shown too — a blocked action is
/// information, not something to hide.
class _ToolRow extends StatefulWidget {
  const _ToolRow({required this.entry});

  final ToolEntry entry;

  @override
  State<_ToolRow> createState() => _ToolRowState();
}

class _ToolRowState extends State<_ToolRow> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final entry = widget.entry;
    final blocked = entry.status == ToolStatus.denied ||
        entry.status == ToolStatus.disabled;

    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        constraints: const BoxConstraints(maxWidth: 560),
        decoration: BoxDecoration(
          color: Hud.glass,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: blocked
                ? Hud.danger.withValues(alpha: 0.45)
                : Hud.border,
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Pressable(
              onTap: entry.hasDetail
                  ? () => setState(() => _expanded = !_expanded)
                  : null,
              builder: (context, hovered) => Padding(
                padding: const EdgeInsets.fromLTRB(11, 9, 10, 9),
                child: Row(
                  children: [
                    _ToolStatusIcon(status: entry.status),
                    const SizedBox(width: 8),
                    Text(
                      entry.name,
                      style: hudMono(
                        size: 11.5,
                        weight: FontWeight.w600,
                        color: blocked ? Hud.danger : Hud.cyan,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        entry.argumentSummary,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: hudMono(size: 11, color: Hud.textDim),
                      ),
                    ),
                    if (entry.hasDetail)
                      Icon(
                        _expanded
                            ? Icons.expand_less_rounded
                            : Icons.expand_more_rounded,
                        size: 16,
                        color: hovered ? Hud.textDim : Hud.textFaint,
                      ),
                  ],
                ),
              ),
            ),
            if (_expanded && entry.hasDetail)
              Container(
                width: double.infinity,
                margin: const EdgeInsets.fromLTRB(11, 0, 11, 11),
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: Hud.bg.withValues(alpha: 0.6),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Hud.border),
                ),
                child: SelectableText(
                  entry.detail!,
                  style: hudMono(
                    size: 11,
                    height: 1.5,
                    color: blocked ? Hud.danger : Hud.textDim,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _ToolStatusIcon extends StatelessWidget {
  const _ToolStatusIcon({required this.status});

  final ToolStatus status;

  @override
  Widget build(BuildContext context) {
    switch (status) {
      case ToolStatus.running:
        return const SizedBox(
          width: 13,
          height: 13,
          child: CircularProgressIndicator(strokeWidth: 1.8),
        );
      case ToolStatus.awaiting:
        return const Icon(Icons.pending_outlined, size: 15, color: Hud.amber);
      case ToolStatus.done:
        return const Icon(Icons.check_circle_outline, size: 15, color: Hud.cyan);
      case ToolStatus.denied:
        return const Icon(Icons.block, size: 15, color: Hud.danger);
      case ToolStatus.disabled:
        return const Icon(Icons.lock_outline, size: 15, color: Hud.textFaint);
    }
  }
}

/// A tool name with its risk tier — the permission layer, made visible.
class _RiskPill extends StatelessWidget {
  const _RiskPill({required this.name, required this.risk});

  final String name;
  final String risk;

  Color get _tint => switch (risk) {
        'read-only' => Hud.cyan,
        'reversible-write' => Hud.violet,
        'destructive' => Hud.amber,
        'external-facing' => Hud.danger,
        _ => Hud.textDim,
      };

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: risk,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(Hud.rPill),
          color: Hud.glass,
          border: Border.all(color: _tint.withValues(alpha: 0.35)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 5,
              height: 5,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: _tint,
              ),
            ),
            const SizedBox(width: 7),
            Text(name, style: hudMono(size: 10.5, color: Hud.text)),
          ],
        ),
      ),
    );
  }
}

// --- palette pieces --------------------------------------------------------

class _PaletteMove extends Intent {
  const _PaletteMove(this.delta);

  final int delta;
}

class _PaletteRun extends Intent {
  const _PaletteRun();
}

class _PaletteRow extends StatelessWidget {
  const _PaletteRow({
    required this.action,
    required this.selected,
    required this.onTap,
    required this.onHover,
  });

  final PaletteAction action;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onHover;

  static IconData iconFor(PaletteAction action) => switch (action.id) {
        'reset' => Icons.refresh_rounded,
        'reconnect' => Icons.sync_rounded,
        'wake' => Icons.hearing_rounded,
        'transcript' => Icons.forum_outlined,
        'settings' => Icons.settings_outlined,
        'ask' => Icons.send_rounded,
        _ => Icons.bolt_rounded,
      };

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      onEnter: (_) => onHover(),
      cursor: SystemMouseCursors.click,
      child: GestureDetector(
        onTap: onTap,
        behavior: HitTestBehavior.opaque,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 120),
          margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 1),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            color: selected ? Hud.glassStrong : Colors.transparent,
          ),
          child: Row(
            children: [
              Icon(
                iconFor(action),
                size: 15,
                color: selected ? Hud.cyan : Hud.textFaint,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      action.label,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: hudUi(size: 13.5, color: Hud.text),
                    ),
                    if (action.detail.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          action.detail,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: hudMono(size: 10.5, color: Hud.textFaint),
                        ),
                      ),
                  ],
                ),
              ),
              if (selected) const _Keycap('↵'),
            ],
          ),
        ),
      ),
    );
  }
}

/// Fades a row into place the first time it appears.
class _Enter extends StatefulWidget {
  const _Enter({required this.child, super.key});

  final Widget child;

  @override
  State<_Enter> createState() => _EnterState();
}

class _EnterState extends State<_Enter> with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 220),
  )..forward();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final curve =
        CurvedAnimation(parent: _controller, curve: Curves.easeOutCubic);
    return FadeTransition(
      opacity: curve,
      child: SlideTransition(
        position: Tween<Offset>(
          begin: const Offset(0, 0.06),
          end: Offset.zero,
        ).animate(curve),
        child: widget.child,
      ),
    );
  }
}
