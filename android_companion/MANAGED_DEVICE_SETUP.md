# Managed-device deployment notes

## Implemented
- DeviceAdminReceiver entry point for Android Enterprise Device Owner / DPC provisioning.
- Boot and app-update receiver schedules network-constrained WorkManager recovery.
- WorkManager sends a background heartbeat when Android schedules it with network available.
- Existing MediaProjection remains explicitly user-started and visibly notified.

## Platform limits
- Factory Reset erases the APK and its data. No ordinary app can prevent this.
- To install again during setup, enroll through Android Enterprise QR provisioning, zero-touch enrollment, or an EMM. The setup wizard then provisions the DPC as Device Owner.
- Device Owner cannot normally be assigned to an already configured personal phone. Use a dedicated test device/emulator and supported provisioning flow.
- Device Owner does not grant silent screen-capture permission. MediaProjection consent is user-mediated and cannot be reused after reboot, force-stop, or process death.
- WorkManager periodic work has a 15-minute minimum interval and is best-effort, not instant.
- Android Settings > Force stop blocks receivers/jobs until the user launches the app again.

## DPC component
    lk.gov.aiplatform.monitor/.MonitorDeviceAdminReceiver

A production enrollment QR must include the final signed APK URL and its actual SHA-256 checksum, plus organization/EMM provisioning fields. These cannot be guessed.

## Test checklist
1. Provision a clean dedicated device as Device Owner.
2. Boot and verify a background heartbeat once network is available.
3. Toggle network off/on and verify the next scheduled heartbeat.
4. Kill the app process and verify background heartbeat recovery (not screen capture).
5. Factory reset; verify app is erased, then enroll again with QR/zero-touch.
6. Open app and manually approve MediaProjection to resume screen sharing.
