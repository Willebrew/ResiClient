import com.denkovi.libs.denkoviHID.Devices.hidDevMCP2200;
import com.denkovi.libs.denkoviHID.Enums.Status;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Persistent, line-oriented controller for a Denkovi 4-channel v2 relay.
 *
 * Protocol on stdin/stdout:
 *   PULSE <relay 1-4> <milliseconds> -> OK <activation latency ms>
 *   PING                              -> OK PONG
 *   QUIT                              -> OK BYE
 *
 * Diagnostics are written to stderr so stdout remains a stable protocol.
 */
public final class RelayDaemon {
    private final String serialNumber;
    private final ScheduledExecutorService scheduler;
    private final ScheduledFuture<?>[] offTasks = new ScheduledFuture<?>[5];
    private final boolean[] activeRelays = new boolean[5];
    private hidDevMCP2200 device;

    private RelayDaemon(String serialNumber) {
        this.serialNumber = serialNumber;
        this.scheduler = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "relay-off-timer");
            thread.setDaemon(true);
            return thread;
        });
    }

    private synchronized void connect() throws Exception {
        closeDevice();
        device = new hidDevMCP2200();
        if (device.openBySerialNumber(serialNumber) != Status.HID_OK) {
            closeDevice();
            throw new IllegalStateException("could not open relay " + serialNumber);
        }

        // This is the same output-direction configuration used by Denkovi's
        // 4v2 CLI. It does not change the current relay output values.
        if (device.WriteIODirections((byte) 0) != Status.HID_OK) {
            closeDevice();
            throw new IllegalStateException("could not configure relay outputs");
        }
        device.ReadInputsOutputs();
    }

    private synchronized void closeDevice() {
        if (device != null) {
            try {
                device.close();
            } catch (Exception ignored) {
                // Best effort during reconnect/shutdown.
            }
            device = null;
        }
    }

    private synchronized void setRelay(int relay, boolean enabled) throws Exception {
        if (relay < 1 || relay > 4) {
            throw new IllegalArgumentException("relay must be between 1 and 4");
        }
        if (device == null) {
            connect();
        }

        try {
            int outputs = device.ReadInputsOutputs() & 0xFF;
            int mask = 1 << (relay - 1);
            int updated = enabled ? outputs | mask : outputs & ~mask;
            if (device.WreiteDefaultOutputs((byte) updated) != Status.HID_OK) {
                throw new IllegalStateException("relay write failed");
            }
            activeRelays[relay] = enabled;
        } catch (Exception firstFailure) {
            // Re-open once for transient USB disconnects, then retry the write.
            connect();
            int outputs = device.ReadInputsOutputs() & 0xFF;
            int mask = 1 << (relay - 1);
            int updated = enabled ? outputs | mask : outputs & ~mask;
            if (device.WreiteDefaultOutputs((byte) updated) != Status.HID_OK) {
                throw firstFailure;
            }
            activeRelays[relay] = enabled;
        }
    }

    private synchronized long pulse(int relay, long durationMs) throws Exception {
        if (durationMs < 1 || durationMs > 60_000) {
            throw new IllegalArgumentException("duration must be 1-60000 ms");
        }

        long started = System.nanoTime();
        setRelay(relay, true);
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);

        ScheduledFuture<?> oldTask = offTasks[relay];
        if (oldTask != null) {
            oldTask.cancel(false);
        }
        offTasks[relay] = scheduler.schedule(() -> {
            try {
                setRelay(relay, false);
            } catch (Exception exception) {
                System.err.println("Failed to turn relay " + relay + " off: " + exception);
            }
        }, durationMs, TimeUnit.MILLISECONDS);

        return elapsedMs;
    }

    private synchronized void shutdown() {
        for (int relay = 1; relay <= 4; relay++) {
            if (offTasks[relay] != null) {
                offTasks[relay].cancel(false);
            }
            if (activeRelays[relay]) {
                try {
                    setRelay(relay, false);
                } catch (Exception exception) {
                    System.err.println("Failed to turn relay " + relay + " off at shutdown: " + exception);
                }
            }
        }
        scheduler.shutdownNow();
        closeDevice();
    }

    private void run() throws Exception {
        connect();
        Runtime.getRuntime().addShutdownHook(new Thread(this::shutdown));
        System.out.println("READY");
        System.out.flush();

        BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
        String line;
        while ((line = reader.readLine()) != null) {
            String[] parts = line.trim().split("\\s+");
            try {
                if (parts.length == 1 && parts[0].equals("PING")) {
                    System.out.println("OK PONG");
                } else if (parts.length == 1 && parts[0].equals("QUIT")) {
                    System.out.println("OK BYE");
                    System.out.flush();
                    return;
                } else if (parts.length == 3 && parts[0].equals("PULSE")) {
                    int relay = Integer.parseInt(parts[1]);
                    long durationMs = Long.parseLong(parts[2]);
                    System.out.println("OK " + pulse(relay, durationMs));
                } else {
                    System.out.println("ERROR invalid command");
                }
            } catch (Exception exception) {
                System.out.println("ERROR " + exception.getMessage());
                System.err.println("Relay command failed: " + exception);
            }
            System.out.flush();
        }
    }

    public static void main(String[] args) {
        if (args.length != 1) {
            System.err.println("Usage: RelayDaemon <serial-number>");
            System.exit(2);
        }

        RelayDaemon daemon = new RelayDaemon(args[0]);
        try {
            daemon.run();
        } catch (Exception exception) {
            System.err.println("Relay daemon startup failed: " + exception);
            System.exit(1);
        } finally {
            daemon.shutdown();
        }
    }
}
