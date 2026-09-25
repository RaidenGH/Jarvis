import 'dart:ui' show FontVariation, ImageFilter;

import 'package:flutter/material.dart';

/// Design tokens lifted from `New UI.html`.
///
/// The mock is the source of truth for the look, so colours, radii and type
/// live here rather than being sprinkled through the widgets. Nothing in the
/// app should hardcode one of these values again.
abstract final class Hud {
  // --- surfaces -----------------------------------------------------------
  static const bg = Color(0xFF0B0D13);
  static const glass = Color(0x0BFFFFFF); // rgba(255,255,255,.045)
  static const glassStrong = Color(0x12FFFFFF); // rgba(255,255,255,.07)
  static const border = Color(0x14FFFFFF); // rgba(255,255,255,.08)
  static const borderStrong = Color(0x29FFFFFF); // rgba(255,255,255,.16)
  static const core = Color(0xFF171B26); // orb centre base
  static const sheet = Color(0xF20E1119); // opaque-enough overlay panel

  // --- type ---------------------------------------------------------------
  static const text = Color(0xFFEEF0F6);
  static const textDim = Color(0xFF9AA2B6);
  static const textFaint = Color(0xFF5B6378);

  // --- accents ------------------------------------------------------------
  static const cyan = Color(0xFF6EE7DD);
  static const violet = Color(0xFFB9A6FF);
  static const amber = Color(0xFFFFB443);
  static const danger = Color(0xFFFF6B81);
  static const cyanSoft = Color(0x296EE7DD); // 16%
  static const violetSoft = Color(0x29B9A6FF); // 16%

  static const ui = 'SpaceGrotesk';
  static const mono = 'JetBrainsMono';

  static const brand = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [cyan, violet],
  );

  /// The gradient behind a user's own message bubble.
  static const outgoing = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [cyanSoft, violetSoft],
  );

  // --- shape --------------------------------------------------------------
  static const rPanel = 18.0;
  static const rBubble = 14.0;
  static const rChip = 8.0;
  static const rPill = 999.0;
}

/// Space Grotesk — the UI face. Weight is applied through the variable font's
/// `wght` axis so we don't need a static file per weight.
TextStyle hudUi({
  double size = 14,
  FontWeight weight = FontWeight.w400,
  Color color = Hud.text,
  double letterSpacing = 0,
  double? height,
}) =>
    TextStyle(
      fontFamily: Hud.ui,
      fontSize: size,
      fontWeight: weight,
      fontVariations: [FontVariation('wght', weight.value.toDouble())],
      color: color,
      letterSpacing: letterSpacing,
      height: height,
    );

/// JetBrains Mono — chips, telemetry, anything that reads as machine output.
TextStyle hudMono({
  double size = 11,
  FontWeight weight = FontWeight.w400,
  Color color = Hud.text,
  double letterSpacing = 0,
  double? height,
}) =>
    TextStyle(
      fontFamily: Hud.mono,
      fontSize: size,
      fontWeight: weight,
      color: color,
      letterSpacing: letterSpacing,
      height: height,
    );

/// Frosted panel: blur what's behind, then lay a translucent fill and a
/// hairline border on top — the mock's `backdrop-filter` recipe.
class Glass extends StatelessWidget {
  const Glass({
    super.key,
    required this.child,
    this.radius = Hud.rPanel,
    this.fill = Hud.glass,
    this.borderColor,
    this.borderWidth = 1,
    this.blur = 18,
    this.padding = EdgeInsets.zero,
  });

  final Widget child;
  final double radius;
  final Color fill;
  final Color? borderColor;
  final double borderWidth;
  final double blur;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) {
    final shape = BorderRadius.circular(radius);
    return ClipRRect(
      borderRadius: shape,
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: blur, sigmaY: blur),
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: fill,
            borderRadius: shape,
            border: Border.all(
              color: borderColor ?? Hud.border,
              width: borderWidth,
            ),
          ),
          child: Padding(padding: padding, child: child),
        ),
      ),
    );
  }
}

/// Desktop-feel tappable: hover tracking, a pointer cursor and a small press
/// dip. Cheaper and better looking than a Material ink splash over glass.
class Pressable extends StatefulWidget {
  const Pressable({
    super.key,
    required this.builder,
    this.onTap,
    this.enabled = true,
  });

  /// [hovered] lets the caller brighten borders/fills on mouse-over.
  final Widget Function(BuildContext context, bool hovered) builder;
  final VoidCallback? onTap;
  final bool enabled;

  @override
  State<Pressable> createState() => _PressableState();
}

class _PressableState extends State<Pressable> {
  bool _hovered = false;
  bool _pressed = false;

  @override
  Widget build(BuildContext context) {
    final live = widget.enabled && widget.onTap != null;
    return MouseRegion(
      cursor: live ? SystemMouseCursors.click : SystemMouseCursors.basic,
      onEnter: (_) => setState(() => _hovered = true),
      onExit: (_) => setState(() => _hovered = false),
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: live ? (_) => setState(() => _pressed = true) : null,
        onTapUp: live ? (_) => setState(() => _pressed = false) : null,
        onTapCancel: live ? () => setState(() => _pressed = false) : null,
        onTap: live ? widget.onTap : null,
        child: AnimatedScale(
          scale: _pressed ? 0.96 : 1,
          duration: const Duration(milliseconds: 110),
          curve: Curves.easeOut,
          child: widget.builder(context, _hovered),
        ),
      ),
    );
  }
}

ThemeData buildHudTheme() {
  const scheme = ColorScheme.dark(
    primary: Hud.cyan,
    onPrimary: Hud.bg,
    secondary: Hud.violet,
    onSecondary: Hud.bg,
    error: Hud.danger,
    onError: Hud.bg,
    surface: Hud.bg,
    onSurface: Hud.text,
    onSurfaceVariant: Hud.textDim,
    outline: Hud.borderStrong,
    outlineVariant: Hud.border,
  );

  final base = ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    fontFamily: Hud.ui,
  );

  return base.copyWith(
    scaffoldBackgroundColor: Hud.bg,
    canvasColor: Hud.bg,
    dividerColor: Hud.border,
    textTheme: base.textTheme.apply(bodyColor: Hud.text, displayColor: Hud.text),
    splashFactory: NoSplash.splashFactory,
    highlightColor: Colors.transparent,
    snackBarTheme: SnackBarThemeData(
      backgroundColor: Hud.sheet,
      contentTextStyle: hudMono(size: 12, color: Hud.text),
      behavior: SnackBarBehavior.floating,
      shape: RoundedRectangleBorder(
        side: const BorderSide(color: Hud.border),
        borderRadius: BorderRadius.circular(Hud.rBubble),
      ),
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: Colors.transparent,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      titleTextStyle: hudUi(size: 17, weight: FontWeight.w600),
      contentTextStyle: hudUi(size: 13.5, color: Hud.textDim, height: 1.5),
    ),
    inputDecorationTheme: InputDecorationTheme(
      isDense: true,
      hintStyle: hudMono(size: 12.5, color: Hud.textFaint),
      border: InputBorder.none,
      enabledBorder: InputBorder.none,
      focusedBorder: InputBorder.none,
    ),
    progressIndicatorTheme: const ProgressIndicatorThemeData(color: Hud.cyan),
    textSelectionTheme: const TextSelectionThemeData(
      cursorColor: Hud.cyan,
      selectionColor: Hud.cyanSoft,
    ),
    tooltipTheme: TooltipThemeData(
      decoration: BoxDecoration(
        color: Hud.sheet,
        borderRadius: BorderRadius.circular(Hud.rChip),
        border: Border.all(color: Hud.border),
      ),
      textStyle: hudMono(size: 10.5, color: Hud.textDim),
      waitDuration: const Duration(milliseconds: 350),
    ),
  );
}
