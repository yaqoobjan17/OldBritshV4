package com.oldbritishcars.aivideo;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Old British Cars - 1-Click Video.
 * Paste script -> tap once -> a GitHub Actions "cloud studio" makes voice, visuals, SFX,
 * animation, subtitles and a 1080p MP4 -> the app downloads it into Movies/OldBritishCars.
 * API keys (Gemini / ElevenLabs / Pexels) live in GitHub Secrets, never on the phone.
 */
public class MainActivity extends Activity {

    static final String WORKFLOW = "render-video.yml";
    static final int GREEN = Color.rgb(27, 94, 32);

    static final String[] VOICE_LABELS = {
            "Auto (ElevenLabs if set, else British male)",
            "British male - Ryan", "British male - Thomas", "British female - Sonia"};
    static final String[] VOICE_ENGINE = {"auto", "edge", "edge", "edge"};
    static final String[] VOICE_NAME = {"en-GB-RyanNeural", "en-GB-RyanNeural", "en-GB-ThomasNeural", "en-GB-SoniaNeural"};

    SharedPreferences prefs;
    EditText script, repoEt, tokenEt;
    TextView status, result;
    ProgressBar bar;
    Button create, openBtn;
    CheckBox subsCb, vintageCb;
    Spinner voiceSp;
    LinearLayout setupBox;
    volatile boolean busy = false;
    Uri savedUri;

    // ------------------------------------------------------------------ UI
    @Override
    public void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("config", 0);
        buildUi();
        String pending = prefs.getString("job", "");
        if (!pending.isEmpty() && configured()) {
            setStatus("Reconnecting to your last video...", 5);
            startThread(pending, null);
        }
    }

    TextView tv(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setPadding(8, 8, 8, 8);
        return t;
    }

    Button btn(String s) {
        Button x = new Button(this);
        x.setText(s);
        x.setTextColor(Color.WHITE);
        x.setBackgroundColor(GREEN);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.setMargins(0, 12, 0, 12);
        x.setLayoutParams(lp);
        return x;
    }

    void buildUi() {
        LinearLayout r = new LinearLayout(this);
        r.setOrientation(LinearLayout.VERTICAL);
        r.setPadding(28, 24, 28, 28);
        ScrollView sv = new ScrollView(this);
        sv.addView(r);
        setContentView(sv);

        TextView h = tv("OLD BRITISH CARS\n1-CLICK VIDEO", 24, GREEN);
        h.setGravity(Gravity.CENTER);
        r.addView(h);
        r.addView(tv("Paste script -> one tap -> voice, visuals, SFX, animation, subtitles -> 1080p MP4", 13, Color.DKGRAY));

        Button setupToggle = new Button(this);
        setupToggle.setText("Setup (GitHub repo + token)");
        r.addView(setupToggle);
        setupBox = new LinearLayout(this);
        setupBox.setOrientation(LinearLayout.VERTICAL);
        setupBox.addView(tv("Repo where you uploaded this project, like  yourname/old-british-cars.  Token = fine-grained GitHub token for ONLY that repo "
                + "(Actions: read+write, Contents: read).", 12, Color.DKGRAY));
        repoEt = new EditText(this);
        repoEt.setHint("owner/repository");
        repoEt.setSingleLine(true);
        repoEt.setText(prefs.getString("repo", ""));
        setupBox.addView(repoEt);
        tokenEt = new EditText(this);
        tokenEt.setHint("GitHub token (github_pat_...)");
        tokenEt.setSingleLine(true);
        tokenEt.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        tokenEt.setText(prefs.getString("token", ""));
        setupBox.addView(tokenEt);
        Button save = btn("SAVE SETUP");
        setupBox.addView(save);
        r.addView(setupBox);
        setupBox.setVisibility(configured() ? View.GONE : View.VISIBLE);
        setupToggle.setOnClickListener(v -> setupBox.setVisibility(setupBox.getVisibility() == View.GONE ? View.VISIBLE : View.GONE));
        save.setOnClickListener(v -> {
            String repo = repoEt.getText().toString().trim().replaceAll("^https?://github.com/", "").replaceAll("/+$", "");
            if (!repo.matches("[\\w.-]+/[\\w.-]+")) {
                toast("Repo must look like  owner/repository");
                return;
            }
            prefs.edit().putString("repo", repo).putString("token", tokenEt.getText().toString().trim()).apply();
            repoEt.setText(repo);
            setupBox.setVisibility(View.GONE);
            toast("Saved");
        });

        script = new EditText(this);
        script.setHint("Paste your video script here...");
        script.setGravity(Gravity.TOP);
        script.setMinLines(10);
        script.setTextSize(16);
        r.addView(script, new LinearLayout.LayoutParams(-1, -2));

        LinearLayout opts = new LinearLayout(this);
        subsCb = new CheckBox(this);
        subsCb.setText("Subtitles");
        subsCb.setChecked(true);
        vintageCb = new CheckBox(this);
        vintageCb.setText("Vintage look");
        vintageCb.setChecked(true);
        opts.addView(subsCb);
        opts.addView(vintageCb);
        r.addView(opts);

        voiceSp = new Spinner(this);
        voiceSp.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, VOICE_LABELS));
        r.addView(voiceSp);

        create = btn("CREATE VIDEO  (1 CLICK)");
        r.addView(create);
        create.setOnClickListener(v -> onCreateClicked());

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setVisibility(View.GONE);
        r.addView(bar);
        status = tv("Ready.", 15, Color.DKGRAY);
        r.addView(status);
        openBtn = btn("PLAY VIDEO");
        openBtn.setVisibility(View.GONE);
        openBtn.setOnClickListener(v -> {
            if (savedUri == null) return;
            Intent i = new Intent(Intent.ACTION_VIEW).setDataAndType(savedUri, "video/mp4").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try {
                startActivity(i);
            } catch (Exception e) {
                toast("No video player found");
            }
        });
        r.addView(openBtn);
        result = tv("", 13, Color.DKGRAY);
        result.setTextIsSelectable(true);
        r.addView(result);
    }

    boolean configured() {
        return !prefs.getString("repo", "").isEmpty() && !prefs.getString("token", "").isEmpty();
    }

    void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    void setStatus(String s, int pct) {
        runOnUiThread(() -> {
            status.setText(s);
            bar.setVisibility(View.VISIBLE);
            bar.setProgress(pct);
        });
    }

    void endJob(boolean ok, String msg, String extra) {
        busy = false;
        runOnUiThread(() -> {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            status.setText(msg);
            result.setText(extra == null ? "" : extra);
            create.setEnabled(true);
            bar.setVisibility(ok ? View.GONE : View.VISIBLE);
            openBtn.setVisibility(ok && savedUri != null ? View.VISIBLE : View.GONE);
        });
    }

    // ------------------------------------------------------------------ job control
    void onCreateClicked() {
        if (busy) return;
        if (!configured()) {
            setupBox.setVisibility(View.VISIBLE);
            toast("Please do the Setup first");
            return;
        }
        String s = script.getText().toString().trim();
        if (s.isEmpty()) {
            toast("Paste a script first");
            return;
        }
        if (s.length() > 60000) {
            toast("Script too long (max about 60,000 characters)");
            return;
        }
        int v = voiceSp.getSelectedItemPosition();
        JSONObject in;
        try {
            in = new JSONObject().put("voice_engine", VOICE_ENGINE[v]).put("voice", VOICE_NAME[v])
                    .put("style", vintageCb.isChecked() ? "vintage" : "clean").put("subtitles", subsCb.isChecked() ? "true" : "false")
                    .put("script", s);
        } catch (Exception e) {
            toast("Could not prepare request");
            return;
        }
        String jobId = "ob" + Long.toString(System.currentTimeMillis() / 1000, 36);
        prefs.edit().putString("job", jobId).apply();
        savedUri = null;
        result.setText("");
        openBtn.setVisibility(View.GONE);
        setStatus("Sending script to the cloud studio...", 3);
        startThread(jobId, in);
    }

    void startThread(String jobId, JSONObject inputs) {
        busy = true;
        create.setEnabled(false);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        new Thread(() -> runJob(jobId, inputs)).start();
    }

    void runJob(String jobId, JSONObject inputs) {
        String repo = prefs.getString("repo", "");
        try {
            if (inputs != null) {
                JSONObject info = new JSONObject(gh("GET", "/repos/" + repo, null));
                inputs.put("job_id", jobId);
                gh("POST", "/repos/" + repo + "/actions/workflows/" + WORKFLOW + "/dispatches",
                        new JSONObject().put("ref", info.getString("default_branch")).put("inputs", inputs).toString());
            }
            setStatus("Cloud studio is starting...", 6);

            // 1) find our run (its title is "Render <jobId>")
            long runId = 0, t0 = System.currentTimeMillis();
            while (runId == 0) {
                Thread.sleep(4000);
                JSONObject runs = new JSONObject(gh("GET", "/repos/" + repo + "/actions/workflows/" + WORKFLOW
                        + "/runs?event=workflow_dispatch&per_page=20", null));
                JSONArray arr = runs.getJSONArray("workflow_runs");
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    if (("Render " + jobId).equals(o.optString("display_title"))) runId = o.getLong("id");
                }
                if (runId == 0 && System.currentTimeMillis() - t0 > 150000)
                    throw new IllegalStateException("The cloud job did not start. Check the repo name, that render-video.yml is uploaded, and token permissions.");
            }

            // 2) follow progress until the run completes
            while (true) {
                JSONObject run = new JSONObject(gh("GET", "/repos/" + repo + "/actions/runs/" + runId, null));
                String failedStep = updateProgress(repo, runId);
                if ("completed".equals(run.getString("status"))) {
                    if (!"success".equals(run.optString("conclusion"))) {
                        prefs.edit().remove("job").apply();
                        endJob(false, "Video failed" + (failedStep.isEmpty() ? "" : " at step " + failedStep),
                                "Open GitHub > your repo > Actions > 'Render " + jobId + "' to read the error. Common causes: empty script, "
                                        + "voice service blocked (add an ElevenLabs key secret), or a typo in a secret.");
                        return;
                    }
                    break;
                }
                Thread.sleep(6000);
            }

            // 3) download the finished MP4 (+ credits)
            setStatus("Downloading your video...", 92);
            JSONObject rel = new JSONObject(gh("GET", "/repos/" + repo + "/releases/tags/video-" + jobId, null));
            String videoUrl = null, credUrl = null;
            JSONArray assets = rel.getJSONArray("assets");
            for (int i = 0; i < assets.length(); i++) {
                JSONObject a = assets.getJSONObject(i);
                if ("final.mp4".equals(a.getString("name"))) videoUrl = a.getString("url");
                if ("credits.txt".equals(a.getString("name"))) credUrl = a.getString("url");
            }
            if (videoUrl == null) throw new IllegalStateException("Finished, but final.mp4 was not found in the release.");
            String where = saveVideo("OldBritishCars_" + jobId + ".mp4", videoUrl);
            String credits = "";
            if (credUrl != null) {
                try {
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    download(credUrl, bo, 0);
                    credits = new String(bo.toByteArray(), StandardCharsets.UTF_8);
                } catch (Exception ignore) {
                    // credits are optional
                }
            }
            prefs.edit().remove("job").apply();
            endJob(true, "Video ready - 1080p MP4 saved:\n" + where, credits);
        } catch (Exception e) {
            String m = e.getMessage() == null ? e.toString() : e.getMessage();
            // keep the pending job (so reopening the app resumes it) only for plain network hiccups
            boolean network = e instanceof IOException && !m.startsWith("HTTP ");
            if (!network) prefs.edit().remove("job").apply();
            endJob(false, "Error: " + m + (network ? "\n(Your video keeps rendering in the cloud - reopen the app to resume.)" : ""), null);
        }
    }

    /** Reads workflow steps "1/4 ... 4/4" and updates the bar. Returns the name of a failed step, or "". */
    String updateProgress(String repo, long runId) {
        try {
            JSONArray jobs = new JSONObject(gh("GET", "/repos/" + repo + "/actions/runs/" + runId + "/jobs", null)).getJSONArray("jobs");
            if (jobs.length() == 0) {
                setStatus("Waiting for a free cloud machine...", 8);
                return "";
            }
            JSONArray steps = jobs.getJSONObject(0).getJSONArray("steps");
            int done = 0;
            String cur = "", failed = "";
            for (int i = 0; i < steps.length(); i++) {
                JSONObject s = steps.getJSONObject(i);
                String n = s.getString("name");
                if (!n.matches("^[1-4]/4 .*")) continue;
                if ("failure".equals(s.optString("conclusion"))) failed = n;
                if ("completed".equals(s.getString("status"))) done++;
                else if ("in_progress".equals(s.getString("status"))) cur = n;
            }
            setStatus(cur.isEmpty() ? "Preparing cloud studio..." : "Step " + cur, 10 + done * 20 + (cur.isEmpty() ? 0 : 8));
            return failed;
        } catch (Exception e) {
            return "";
        }
    }

    // ------------------------------------------------------------------ saving
    String saveVideo(String name, String assetUrl) throws Exception {
        if (Build.VERSION.SDK_INT >= 29) {
            ContentValues cv = new ContentValues();
            cv.put(MediaStore.Video.Media.DISPLAY_NAME, name);
            cv.put(MediaStore.Video.Media.MIME_TYPE, "video/mp4");
            cv.put(MediaStore.Video.Media.RELATIVE_PATH, Environment.DIRECTORY_MOVIES + "/OldBritishCars");
            cv.put(MediaStore.Video.Media.IS_PENDING, 1);
            Uri uri = getContentResolver().insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
            if (uri == null) throw new IOException("Could not create a file in Movies");
            try (OutputStream o = getContentResolver().openOutputStream(uri)) {
                download(assetUrl, o, 92);
            }
            ContentValues fin = new ContentValues();
            fin.put(MediaStore.Video.Media.IS_PENDING, 0);
            getContentResolver().update(uri, fin, null, null);
            savedUri = uri;
            return "Movies/OldBritishCars/" + name;
        }
        File dir = getExternalFilesDir(Environment.DIRECTORY_MOVIES);
        if (dir == null) throw new IOException("No storage available");
        dir.mkdirs();
        File f = new File(dir, name);
        try (FileOutputStream o = new FileOutputStream(f)) {
            download(assetUrl, o, 92);
        }
        return f.getAbsolutePath();
    }

    // ------------------------------------------------------------------ HTTP
    HttpURLConnection open(String url, String method) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(30000);
        c.setReadTimeout(120000);
        return c;
    }

    String gh(String method, String path, String body) throws Exception {
        HttpURLConnection c = open("https://api.github.com" + path, method);
        c.setRequestProperty("Authorization", "Bearer " + prefs.getString("token", ""));
        c.setRequestProperty("Accept", "application/vnd.github+json");
        c.setRequestProperty("X-GitHub-Api-Version", "2022-11-28");
        c.setRequestProperty("User-Agent", "OldBritishCars-App");
        if (body != null) {
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = c.getOutputStream()) {
                o.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = c.getResponseCode();
        InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
        String txt = in == null ? "" : new String(readAll(in), StandardCharsets.UTF_8);
        if (code >= 400) throw new IOException(explain(code, txt));
        return txt;
    }

    String explain(int code, String txt) {
        String msg = "";
        try {
            msg = new JSONObject(txt).optString("message");
        } catch (Exception ignore) {
            // body was not JSON
        }
        if (code == 401) return "HTTP 401 - token is wrong or expired. Make a new one in Setup.";
        if (code == 403 || code == 404)
            return "HTTP " + code + " - no access. Check the repo name and that the token has Actions (read+write) and Contents (read) for this repo. " + msg;
        if (code == 422) return "HTTP 422 - " + msg + " (is render-video.yml on the default branch?)";
        return "HTTP " + code + " " + msg;
    }

    /** Downloads a private release asset. GitHub answers with a redirect to a signed URL that must NOT carry the token. */
    void download(String assetApiUrl, OutputStream out, int progressBase) throws Exception {
        HttpURLConnection c = open(assetApiUrl, "GET");
        c.setRequestProperty("Authorization", "Bearer " + prefs.getString("token", ""));
        c.setRequestProperty("Accept", "application/octet-stream");
        c.setRequestProperty("User-Agent", "OldBritishCars-App");
        c.setInstanceFollowRedirects(false);
        int code = c.getResponseCode();
        if (code >= 300 && code < 400) {
            String loc = c.getHeaderField("Location");
            c.disconnect();
            c = open(loc, "GET");
            code = c.getResponseCode();
        }
        if (code >= 400) throw new IOException("HTTP " + code + " while downloading");
        long total = 0, got = 0;
        try {
            total = Long.parseLong(c.getHeaderField("Content-Length"));
        } catch (Exception ignore) {
            // unknown size: no percentage
        }
        byte[] buf = new byte[1 << 16];
        int n, lastPct = -1;
        try (InputStream in = c.getInputStream()) {
            while ((n = in.read(buf)) != -1) {
                out.write(buf, 0, n);
                got += n;
                if (progressBase > 0 && total > 0) {
                    int pct = progressBase + (int) (7 * got / total);
                    if (pct != lastPct) {
                        lastPct = pct;
                        setStatus("Downloading your video... " + (100 * got / total) + "%", pct);
                    }
                }
            }
        }
    }

    byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) o.write(buf, 0, n);
        return o.toByteArray();
    }
}
