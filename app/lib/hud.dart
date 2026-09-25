import 'dart:async';
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'theme.dart';

/// What the orb is doing — the mock's four states, plus `offline` so a dead
/// backend reads differently from a quiet one.
enum OrbState { idle, listening, thinking, speaking, offline }

/// The ambient backdrop: two soft glow fields bleeding in from opposite
/// corners over a film grain. Pure decoration — no grid, no scanline, no
/// corner brackets in this design.
class HudBackdrop extends StatefulWidget {
  const HudBackdrop({super.key});

  @override
  State<HudBackdrop> createState() => _HudBackdropState();
}

class _HudBackdropState extends State<HudBackdrop> {
  ui.Image? _grain;

  @override
  void initState() {
    super.initState();
    _buildGrain().then((image) {
      if (mounted) setState(() => _grain = image);
    });
  }

  /// A 90x90 tile of white noise, generated once and tiled across the window.
  /// Cheaper and steadier than painting thousands of dots every frame.
  Future<ui.Image> _buildGrain() {
    const size = 90;
    final rnd = math.Random(7);
    final pixels = Uint8List(size * size * 4);
    for (var i = 0; i < pixels.length; i += 4) {
      final v = rnd.nextInt(256);
      pixels[i] = v;
      pixels[i + 1] = v;
      pixels[i + 2] = v;
      pixels[i + 3] = 255;
    }
    final completer = Completer<ui.Image>();
    ui.decodeImageFromPixels(
      pixels,
      size,
      size,
      ui.PixelFormat.rgba8888,
      completer.complete,
    );
    return completer.future;
  }

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: Stack(
        fit: StackFit.expand,
        children: [
          const ColoredBox(color: Hud.bg),
          const CustomPaint(painter: _GlowPainter()),
          if (_grain != null)
            Opacity(
              opacity: 0.03,
              child: RawImage(
                image: _grain,
                repeat: ImageRepeat.repeat,
                fit: BoxFit.none,
                alignment: Alignment.topLeft,
              ),
            ),
        ],
      ),
    );
  }
}

/// `.glow.a` / `.glow.b` from the mock: big blurred radial fields, cyan from
/// the top-left corner and violet from the bottom-right.
class _GlowPainter extends CustomPainter {
  const _GlowPainter();

  void _field(Canvas canvas, Size size, Offset center, double radius, Color color) {
    canvas.drawCircle(
      center,
      radius,
      Paint()
        ..shader = RadialGradient(
          colors: [color, color.withValues(alpha: 0)],
          stops: const [0.0, 0.7],
        ).createShader(Rect.fromCircle(center: center, radius: radius)),
    );
  }

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    // .glow.a — 46vw, centred at 15vw / 9vw, opacity .55
    _field(
      canvas,
      size,
      Offset(w * 0.15, size.height * 0.06),
      w * 0.23,
      Hud.cyan.withValues(alpha: 0.30),
    );
    // .glow.b — 50vw, centred at 87vw / 95vw, opacity .4
    _field(
      canvas,
      size,
      Offset(w * 0.87, size.height * 0.96),
      w * 0.25,
      Hud.violet.withValues(alpha: 0.22),
    );
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

/// The centrepiece: a breathing core inside two rings and a halo of 36 level
/// bars. Mirrors the mock's JS — same state table, same bar maths.
class Orb extends StatefulWidget {
  const Orb({
    super.key,
    required this.state,
    this.size = 208,
    this.energy,
  });

  final OrbState state;

  /// Design size; the mock draws a 208px wrap and everything scales from it.
  final double size;

  /// Live loudness 0..1 for the speaking bars. Fed by the reply token stream
  /// so the orb moves with the actual answer instead of a canned loop.
  final ValueListenable<double>? energy;

  @override
  State<Orb> createState() => _OrbState();
}

class _OrbState extends State<Orb> with TickerProviderStateMixin {
  static const _design = 208.0;
  static const _barCount = 36;
  static const _barRadius = 92.0;

  final ValueNotifier<double> _tick = ValueNotifier(0);

  late final Ticker _ticker;
  late final AnimationController _accent = AnimationController(
    vsync: this,
    duration: const Duration(seconds: 5),
  )..repeat();
  late final AnimationController _thinking = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1700),
  );
  late final AnimationController _breathe = AnimationController(
    vsync: this,
    duration: const Duration(seconds: 4),
  );

  @override
  void initState() {
    super.initState();
    _ticker = createTicker((elapsed) {
      // The mock counts animation frames; 60fps is the same clock.
      _tick.value = elapsed.inMicroseconds / 16666.67;
    })
      ..start();
    _sync();
  }

  @override
  void didUpdateWidget(covariant Orb old) {
    super.didUpdateWidget(old);
    if (old.state != widget.state) _sync();
  }

  void _sync() {
    if (widget.state == OrbState.thinking) {
      _thinking.repeat();
    } else {
      _thinking.stop();
      _thinking.value = 0;
    }
    if (widget.state == OrbState.idle || widget.state == OrbState.offline) {
      _breathe.repeat(reverse: true);
    } else {
      _breathe.stop();
      _breathe.value = 0;
    }
  }

  @override
  void dispose() {
    _ticker.dispose();
    _accent.dispose();
    _thinking.dispose();
    _breathe.dispose();
    _tick.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final size = widget.size;
    final state = widget.state;
    final dim = state == OrbState.offline;
    final thinking = state == OrbState.thinking;
    final scale = size / _design;

    return SizedBox(
      width: size,
      height: size,
      child: Opacity(
        opacity: dim ? 0.45 : 1,
        child: Stack(
          alignment: Alignment.center,
          children: [
            // Static hairline ring.
            DecoratedBox(
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(color: Hud.border),
              ),
              child: SizedBox.square(dimension: size),
            ),
            // Slow accent sweep: hidden while the thinking ring is up.
            AnimatedOpacity(
              opacity: thinking ? 0 : 0.55,
              duration: const Duration(milliseconds: 220),
              child: RotationTransition(
                turns: _accent,
                child: CustomPaint(
                  size: Size.square(size),
                  painter: _AccentRingPainter(radius: size / 2 - 0.75),
                ),
              ),
            ),
            // Fast counter-rotating ring, only while thinking.
            if (thinking)
              RotationTransition(
                turns: _thinking,
                child: CustomPaint(
                  size: Size.square(size),
                  painter: _ThinkingRingPainter(radius: size / 2 - 0.75),
                ),
              ),
            // Level bars.
            CustomPaint(
              size: Size.square(size),
              painter: _BarsPainter(
                tick: _tick,
                energy: widget.energy,
                state: state,
                scale: scale,
                dim: dim,
                radius: _barRadius * scale,
                count: _barCount,
              ),
            ),
            // Breathing core.
            AnimatedBuilder(
              animation: _breathe,
              builder: (context, child) => Transform.scale(
                scale: 1 + 0.04 * _breathe.value,
                child: _OrbCore(
                  size: 96 * scale,
                  dim: dim,
                  // Stands in for the mock's `filter: brightness(1.14)`.
                  highlight: _breathe.value,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _OrbCore extends StatelessWidget {
  const _OrbCore({required this.size, this.dim = false, this.highlight = 0});

  final double size;
  final bool dim;

  /// 0..1 breathing brightness.
  final double highlight;

  @override
  Widget build(BuildContext context) {
    return SizedBox.square(
      dimension: size,
      child: DecoratedBox(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: Hud.core,
          boxShadow: [
            BoxShadow(
              color: Hud.cyan.withValues(alpha: dim ? 0.06 : 0.22),
              blurRadius: size * 0.46,
              spreadRadius: size * 0.02,
            ),
            BoxShadow(
              color: Hud.violet.withValues(alpha: dim ? 0.04 : 0.12),
              blurRadius: size * 0.94,
            ),
          ],
        ),
        child: ClipOval(
          child: Stack(
            fit: StackFit.expand,
            children: [
              // radial-gradient(circle at 66% 68%, violet, transparent 60%)
              DecoratedBox(
                decoration: BoxDecoration(
                  gradient: RadialGradient(
                    center: const Alignment(0.32, 0.36),
                    radius: 1.1,
                    colors: [
                      Hud.violet.withValues(alpha: 0.55),
                      Hud.violet.withValues(alpha: 0),
                    ],
                    stops: const [0.0, 0.6],
                  ),
                ),
              ),
              // radial-gradient(circle at 34% 66%, cyan, transparent 58%)
              DecoratedBox(
                decoration: BoxDecoration(
                  gradient: RadialGradient(
                    center: const Alignment(-0.32, 0.32),
                    radius: 1.1,
                    colors: [
                      Hud.cyan.withValues(alpha: 0.55),
                      Hud.cyan.withValues(alpha: 0),
                    ],
                    stops: const [0.0, 0.58],
                  ),
                ),
              ),
              // radial-gradient(circle at 32% 28%, white 30%, transparent 45%)
              DecoratedBox(
                decoration: BoxDecoration(
                  gradient: RadialGradient(
                    center: const Alignment(-0.36, -0.44),
                    radius: 0.95,
                    colors: [
                      Colors.white.withValues(alpha: 0.30),
                      Colors.white.withValues(alpha: 0),
                    ],
                    stops: const [0.0, 0.45],
                  ),
                ),
              ),
              if (highlight > 0)
                ColoredBox(
                  color: Colors.white.withValues(alpha: 0.10 * highlight),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// `.orb-ring.accent`: a 1.5px ring whose top quarter is cyan and right
/// quarter violet, spinning slowly.
class _AccentRingPainter extends CustomPainter {
  const _AccentRingPainter({required this.radius});

  final double radius;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Rect.fromCircle(center: size.center(Offset.zero), radius: radius);
    const start = -math.pi * 0.75; // top-left corner of the top quarter
    canvas.drawArc(
      rect,
      start,
      math.pi, // top quarter into the right quarter
      false,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.5
        ..shader = SweepGradient(
          startAngle: start,
          endAngle: start + math.pi,
          colors: const [Hud.cyan, Hud.violet],
        ).createShader(rect),
    );
  }

  @override
  bool shouldRepaint(covariant _AccentRingPainter old) => old.radius != radius;
}

/// `.orb-ring.thinking-ring`: violet top arc, cyan left arc, spinning fast.
class _ThinkingRingPainter extends CustomPainter {
  const _ThinkingRingPainter({required this.radius});

  final double radius;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Rect.fromCircle(center: size.center(Offset.zero), radius: radius);
    final paint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5
      ..strokeCap = StrokeCap.round;
    // Top quarter.
    canvas.drawArc(rect, -math.pi * 0.75, math.pi / 2, false, paint..color = Hud.violet);
    // Left quarter.
    canvas.drawArc(rect, math.pi * 0.75, math.pi / 2, false, paint..color = Hud.cyan);
  }

  @override
  bool shouldRepaint(covariant _ThinkingRingPainter old) => old.radius != radius;
}

/// The 36 radial level bars, positioned exactly like the mock: each bar hangs
/// inward from a point 92px (design space) out from the centre.
class _BarsPainter extends CustomPainter {
  _BarsPainter({
    required this.tick,
    required this.energy,
    required this.state,
    required this.scale,
    required this.radius,
    required this.count,
    required this.dim,
  }) : super(repaint: tick);

  final ValueListenable<double> tick;
  final ValueListenable<double>? energy;
  final OrbState state;
  final double scale;
  final double radius;
  final int count;
  final bool dim;

  /// The mock's height tables, with a smooth per-bar wobble in place of
  /// `Math.random()` so bars shimmer instead of strobing.
  double _height(int i, double t) {
    final wobble = 0.5 + 0.5 * math.sin(t * 1.7 + i * 2.3);
    switch (state) {
      case OrbState.listening:
        final n = math.sin(t * 0.11 + i * 0.7) * 0.5 + math.sin(t * 0.045 + i) * 0.5;
        return 7 + n.abs() * 24 + wobble * 5;
      case OrbState.speaking:
        final n = math.sin(t * 0.2 + i * 0.4);
        return 9 + n.abs() * 30 + wobble * 3;
      case OrbState.thinking:
        return 5;
      case OrbState.idle:
      case OrbState.offline:
        return 7;
    }
  }

  @override
  void paint(Canvas canvas, Size size) {
    final t = tick.value;
    final centre = size.center(Offset.zero);
    final width = 3 * scale;
    final r = radius;

    for (var i = 0; i < count; i++) {
      var h = _height(i, t);
      if (state == OrbState.speaking) {
        // Reply-stream loudness: quiet passages shrink toward a resting hum,
        // loud ones stretch past the canned loop.
        final e = (energy?.value ?? 0.6).clamp(0.0, 1.0);
        h *= 0.45 + 0.85 * e;
      }
      h *= scale;

      final rect = Rect.fromLTWH(-width / 2, -r, width, h);
      final paint = Paint()
        ..color = Colors.white
        ..shader = ui.Gradient.linear(
          Offset(0, -r),
          Offset(0, -r + math.max(h, 1)),
          dim
              ? [Hud.textFaint, Hud.textFaint.withValues(alpha: 0.2)]
              : [Hud.cyan, Hud.violet],
        )
        ..strokeWidth = width;

      canvas.save();
      canvas.translate(centre.dx, centre.dy);
      canvas.rotate((i / count) * 2 * math.pi);
      canvas.drawRRect(
        RRect.fromRectAndRadius(rect, Radius.circular(width / 2)),
        paint,
      );
      canvas.restore();
    }
  }

  @override
  bool shouldRepaint(covariant _BarsPainter old) =>
      old.state != state ||
      old.scale != scale ||
      old.radius != radius ||
      old.dim != dim;
}
