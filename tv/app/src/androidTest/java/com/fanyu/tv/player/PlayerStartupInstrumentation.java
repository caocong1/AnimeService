package com.fanyu.tv.player;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.SystemClock;
import java.io.*;
import java.net.*;
import java.util.concurrent.*;

/** Signed-release smoke test. All HTTP traffic goes to a synthetic in-process loopback server. */
public final class PlayerStartupInstrumentation extends Instrumentation {
    @Override public void onCreate(Bundle arguments) { super.onCreate(arguments); start(); }
    @Override public void onStart() {
        Bundle result = new Bundle();
        FixtureServer server = null;
        SharedPreferences prefs = getTargetContext().getSharedPreferences("fanyu", 0);
        String previous = prefs.getString("server", "");
        try {
            InputStream input = getContext().getAssets().open("playback-smoke.mp4");
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] chunk = new byte[4096];
            int count;
            try { while ((count = input.read(chunk)) != -1) output.write(chunk, 0, count); }
            finally { input.close(); }
            server = new FixtureServer(output.toByteArray());
            final FixtureServer fixtureServer = server;
            prefs.edit().putString("server", "http://127.0.0.1:" + server.port()).commit();
            for (int show = 1; show <= 2; show++) {
                final int previousReports = fixtureServer.progressReports;
                final Activity activity = startActivitySync(new Intent()
                    .setClassName(getTargetContext(), "com.fanyu.tv.player.PlayerActivity")
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK).putExtra("show", show).putExtra("episode", 1));
                Thread.sleep(3000);
                sendKeyDownUpSync(android.view.KeyEvent.KEYCODE_DPAD_RIGHT);
                Thread.sleep(2500);
                runOnMainSync(activity::finish);
                waitForIdleSync();
                // Uses the normal public progress API: real playback must have occurred, then
                // advanced beyond the ten-second remote-control seek. No obfuscated member access.
                await("playback saved after seek", () -> fixtureServer.progressReports > previousReports && fixtureServer.lastPosition > 12);
            }
            result.putString("stream", "PASS: signed release opened two synthetic titles, played, sought, resumed and released\n");
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("stream", "FAIL: " + error.getClass().getSimpleName() + ": " + error.getMessage() + "\n");
            finish(Activity.RESULT_CANCELED, result);
        } finally {
            prefs.edit().putString("server", previous).commit();
            if (server != null) server.close();
        }
    }
    private interface Condition { boolean get(); }
    private void await(String description, Condition condition) throws InterruptedException {
        long deadline = SystemClock.elapsedRealtime() + 20000;
        while (SystemClock.elapsedRealtime() < deadline) {
            if (condition.get()) return;
            Thread.sleep(100);
        }
        throw new AssertionError("Timed out waiting for " + description);
    }
    private static final class FixtureServer {
        private final byte[] video;
        private final ServerSocket socket;
        private final ExecutorService workers = Executors.newCachedThreadPool();
        volatile int progressReports;
        volatile double lastPosition;
        FixtureServer(byte[] video) throws IOException {
            this.video = video;
            socket = new ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"));
            workers.execute(() -> {
                while (!socket.isClosed()) {
                    try { final Socket client = socket.accept(); workers.execute(() -> respond(client)); }
                    catch (IOException ignored) { break; }
                }
            });
        }
        int port() { return socket.getLocalPort(); }
        private void respond(Socket client) {
            try {
                client.setSoTimeout(5000);
                BufferedReader reader = new BufferedReader(new InputStreamReader(client.getInputStream(), "UTF-8"));
                String[] request = reader.readLine().split(" ");
                String path = request[1], range = null, line;
                int contentLength = 0;
                while ((line = reader.readLine()) != null && !line.isEmpty()) {
                    if (line.regionMatches(true, 0, "Range:", 0, 6)) range = line.substring(6).trim();
                    if (line.regionMatches(true, 0, "Content-Length:", 0, 15)) contentLength = Integer.parseInt(line.substring(15).trim());
                }
                if (path.endsWith("/progress") && contentLength > 0) {
                    char[] chars = new char[contentLength];
                    int read = 0, n;
                    while (read < chars.length && (n = reader.read(chars, read, chars.length - read)) > 0) read += n;
                    lastPosition = new org.json.JSONObject(new String(chars, 0, read)).getDouble("position");
                    progressReports++;
                }
                String status = "200 OK", type = "application/json", extra = "", json = "{}";
                byte[] body;
                if (path.startsWith("/api/shows/")) {
                    json = "{\"show\":{\"id\":1,\"title\":\"Synthetic playback test\",\"total\":1},\"board\":{\"eps\":[{\"n\":1,\"s\":\"ready\",\"media\":\"synthetic\"}]}}";
                } else if (path.equals("/api/web/media/synthetic")) {
                    json = "{\"size\":" + video.length + ",\"progress\":{}}";
                } else if (path.equals("/api/bootstrap")) json = "{\"token\":\"synthetic-only\"}";
                if (path.equals("/api/web/media/synthetic/stream")) {
                    type = "video/mp4";
                    extra = "Accept-Ranges: bytes\r\n";
                    body = video;
                    if (range != null) {
                        String[] parts = range.substring(6).split("-", -1);
                        int start = Integer.parseInt(parts[0]);
                        int end = parts[1].isEmpty() ? video.length - 1 : Integer.parseInt(parts[1]);
                        body = java.util.Arrays.copyOfRange(video, start, end + 1);
                        status = "206 Partial Content";
                        extra += "Content-Range: bytes " + start + "-" + end + "/" + video.length + "\r\n";
                    }
                } else body = json.getBytes("UTF-8");
                OutputStream out = client.getOutputStream();
                out.write(("HTTP/1.1 " + status + "\r\nContent-Type: " + type + "\r\nContent-Length: " + body.length +
                    "\r\n" + extra + "Connection: close\r\n\r\n").getBytes("UTF-8"));
                out.write(body);
                out.flush();
            } catch (Exception ignored) { /* A seek/close may cancel a fixture request. */ }
            finally { try { client.close(); } catch (IOException ignored) {} }
        }
        void close() { try { socket.close(); } catch (IOException ignored) {} workers.shutdownNow(); }
    }
}
