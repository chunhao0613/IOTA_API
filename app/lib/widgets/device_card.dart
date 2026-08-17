import 'package:flutter/material.dart';

import '../theme/app_theme.dart';
import '../utils/parsing.dart';

/// 裝置卡片上可觸發的管理動作。
enum DeviceAction { control, maintenance, otaUpdate, decommission }

/// 場域裝置卡片。
///
/// 同時被家庭詳情頁的「設備狀態」分頁與場域儀表板使用，因此資料來源可能是
/// `/list_devices`（欄位較少）或 `/dashboard`（多了 battery / rssi /
/// connection_health / last_command）。缺少的欄位一律以「—」呈現，不做假資料。
class DeviceCard extends StatelessWidget {
  const DeviceCard({
    super.key,
    required this.device,
    required this.isAdmin,
    this.canControl = true,
    this.onAction,
  });

  final Map<String, dynamic> device;
  final bool isAdmin;

  /// Guest 在後端會被 `control_device` 的角色檢查擋掉（403），
  /// 這裡先在 UI 隱藏，避免給出注定失敗的操作。
  final bool canControl;

  final void Function(DeviceAction action)? onAction;

  bool get _isRetired {
    final status = asText(device['status'], fallback: '').toLowerCase();
    return status == 'revoked' || status == 'retired' || status == 'decommissioned';
  }

  bool get _inMaintenance => asBool(device['maintenance_mode']);

  @override
  Widget build(BuildContext context) {
    final deviceId = asText(device['device_id'], fallback: '—');
    final name = asText(device['device_name'], fallback: '未命名裝置');

    return Container(
      margin: const EdgeInsets.only(bottom: AppSpacing.md),
      decoration: BoxDecoration(
        color: AppColors.surface.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(AppRadius.xl),
        border: Border.all(
          color: _isRetired
              ? AppColors.danger.withValues(alpha: 0.25)
              : _inMaintenance
                  ? AppColors.warning.withValues(alpha: 0.35)
                  : AppColors.border,
        ),
      ),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(AppRadius.xl),
          onTap: (_isRetired || !canControl)
              ? null
              : () => onAction?.call(DeviceAction.control),
          child: Padding(
            padding: const EdgeInsets.all(AppSpacing.md),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    _buildIcon(),
                    const SizedBox(width: AppSpacing.md - 4),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            name,
                            style: TextStyle(
                              color: _isRetired
                                  ? AppColors.textTertiary
                                  : AppColors.textPrimary,
                              fontSize: 16,
                              fontWeight: FontWeight.bold,
                              decoration: _isRetired
                                  ? TextDecoration.lineThrough
                                  : null,
                            ),
                            overflow: TextOverflow.ellipsis,
                          ),
                          const SizedBox(height: 2),
                          Text(
                            deviceId,
                            style: TextStyle(
                              color: AppColors.textDisabled,
                              fontSize: 11,
                              fontFamily: 'monospace',
                            ),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ],
                      ),
                    ),
                    if (isAdmin && onAction != null) _buildAdminMenu(context),
                  ],
                ),
                const SizedBox(height: AppSpacing.md - 4),
                _buildBadges(),
                if (_inMaintenance) ...[
                  const SizedBox(height: AppSpacing.sm),
                  _buildMaintenanceLine(),
                ],
                if (!_isRetired && canControl) ...[
                  const SizedBox(height: AppSpacing.sm),
                  Row(
                    children: [
                      Icon(Icons.touch_app_outlined,
                          size: 13, color: AppColors.textDisabled),
                      const SizedBox(width: AppSpacing.xs),
                      Text(
                        '點擊卡片進行遠端控制',
                        style: TextStyle(
                            color: AppColors.textDisabled, fontSize: 11),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildIcon() {
    final type = asText(device['device_type'], fallback: '').toLowerCase();
    final icon = switch (type) {
      'smart_lock' => Icons.lock_outline,
      'sensor' => Icons.sensors,
      'camera' => Icons.videocam_outlined,
      _ => Icons.router_outlined,
    };

    final color = _isRetired
        ? AppColors.textDisabled
        : _inMaintenance
            ? AppColors.warning
            : AppColors.purpleLight;

    return Container(
      width: 44,
      height: 44,
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(AppRadius.md),
      ),
      child: Icon(icon, color: color, size: 22),
    );
  }

  Widget _buildBadges() {
    final badges = <Widget>[];

    // 連線健康度（只有 /dashboard 會回）
    final health = asText(device['connection_health'], fallback: '');
    if (health.isNotEmpty) {
      final (color, label) = switch (health.toUpperCase()) {
        'GOOD' => (AppColors.healthGood, '連線良好'),
        'WEAK' => (AppColors.healthWeak, '訊號弱'),
        'OFFLINE' => (AppColors.healthOffline, '離線'),
        'FAULT' => (AppColors.healthFault, '異常'),
        'NO_DATA' => (AppColors.healthOffline, '尚無回報'),
        _ => (AppColors.healthOffline, '狀態未知'),
      };
      badges.add(_badge(label, color, Icons.circle, iconSize: 8));
    }

    // 實體狀態
    final state = asText(device['physical_state'], fallback: '');
    if (state.isNotEmpty) {
      final isUnlocked = state.toLowerCase() == 'unlocked';
      badges.add(_badge(
        physicalStateLabel(state),
        isUnlocked ? AppColors.warning : AppColors.success,
        isUnlocked ? Icons.lock_open_rounded : Icons.lock_rounded,
      ));
    }

    // 電量
    final battery = asInt(device['battery']);
    if (battery != null) {
      final lowBattery = asBool(device['low_battery']) || battery <= 20;
      badges.add(_badge(
        '$battery%',
        lowBattery ? AppColors.danger : AppColors.textSecondary,
        lowBattery ? Icons.battery_alert_rounded : Icons.battery_full_rounded,
      ));
    }

    // 訊號強度
    final rssi = asInt(device['rssi']);
    if (rssi != null) {
      badges.add(_badge('$rssi dBm', AppColors.textSecondary, Icons.wifi));
    }

    // 除役狀態
    if (_isRetired) {
      badges.add(_badge('已除役', AppColors.danger, Icons.block));
    }

    // 配對狀態（/list_devices 才有）
    final pairing = asText(device['pairing_status'], fallback: '');
    if (pairing.isNotEmpty && !_isRetired) {
      badges.add(_badge(
        pairing == 'paired' ? '已配對' : pairing,
        pairing == 'paired' ? AppColors.success : AppColors.textTertiary,
        Icons.link,
      ));
    }

    // 最後回報時間
    final lastSeen = device['last_seen_at'] ?? device['last_update'];
    if (lastSeen != null) {
      badges.add(_badge(
        relativeTime(lastSeen),
        AppColors.textTertiary,
        Icons.schedule,
      ));
    }

    if (badges.isEmpty) {
      return Text(
        '尚無狀態資料',
        style: TextStyle(color: AppColors.textDisabled, fontSize: 12),
      );
    }

    return Wrap(
      spacing: AppSpacing.sm,
      runSpacing: AppSpacing.sm,
      children: badges,
    );
  }

  Widget _badge(String label, Color color, IconData icon,
      {double iconSize = 12}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: iconSize, color: color),
          const SizedBox(width: AppSpacing.xs),
          Text(
            label,
            style: TextStyle(
                color: color, fontSize: 11, fontWeight: FontWeight.bold),
          ),
        ],
      ),
    );
  }

  Widget _buildMaintenanceLine() {
    return Row(
      children: [
        const Icon(Icons.build_circle_outlined,
            size: 13, color: AppColors.warning),
        const SizedBox(width: AppSpacing.xs),
        Expanded(
          child: Text(
            '維修模式中，日常控制已暫停'
            '${device['maintenance_expires_at'] != null ? '（至 ${formatDisplay(device['maintenance_expires_at'])}）' : ''}',
            style: const TextStyle(color: AppColors.warning, fontSize: 11),
            overflow: TextOverflow.ellipsis,
          ),
        ),
      ],
    );
  }

  Widget _buildAdminMenu(BuildContext context) {
    return PopupMenuButton<DeviceAction>(
      icon: Icon(Icons.more_vert, color: AppColors.textTertiary),
      color: AppColors.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadius.md),
      ),
      onSelected: (action) => onAction?.call(action),
      itemBuilder: (context) => [
        if (!_isRetired)
          PopupMenuItem(
            value: DeviceAction.maintenance,
            child: _menuRow(
              _inMaintenance ? Icons.build_circle : Icons.build_circle_outlined,
              _inMaintenance ? '關閉維修模式' : '開啟維修模式',
              AppColors.warning,
            ),
          ),
        if (!_isRetired)
          PopupMenuItem(
            value: DeviceAction.otaUpdate,
            child: _menuRow(
              Icons.system_update_alt,
              '韌體更新',
              AppColors.info,
            ),
          ),
        if (!_isRetired)
          PopupMenuItem(
            value: DeviceAction.decommission,
            child: _menuRow(Icons.delete_outline, '除役與解綁', AppColors.danger),
          ),
        if (_isRetired)
          PopupMenuItem(
            enabled: false,
            value: DeviceAction.control,
            child: _menuRow(
                Icons.block, '此裝置已除役', AppColors.textDisabled),
          ),
      ],
    );
  }

  Widget _menuRow(IconData icon, String label, Color color) => Row(
        children: [
          Icon(icon, size: 18, color: color),
          const SizedBox(width: AppSpacing.sm),
          Text(label, style: TextStyle(color: color, fontSize: 14)),
        ],
      );
}
