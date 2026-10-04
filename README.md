# Immich Age Guesser

Put dates on scanned analog photos in [Immich](https://immich.app).

The tool handles photos in batches, one Immich album per batch of scans:

- **You know the date** (even just the year or month): type `1987`, `06.1987`, `1985-1989` or
  `1980s`, and it is written to the selected photos.
- **You don't know the date**: the tool estimates it from the people in the photo. It estimates how
  old each person looks, and combines that with their birthday in Immich.
  `birthday + apparent age` gives a date. Several people in one photo, or several photos from the
  same event, narrow the range. You review the suggestions and then write them to Immich.

Everything runs locally. Photos never leave your server.

## How the estimate works

1. Immich has already found the faces (bounding boxes) and you have named the people.
2. Each named face with a known birthday is cropped from Immich's preview. Small faces in group
   photos are cropped from the full-resolution scan. An age model estimates how old the person looks:
   [MiVOLO v2](https://github.com/WildChlamydia/MiVOLO) by default, or a vision LLM via Ollama.
3. For every possible date *t*, the person's true age would be *t − birthday*. The model's error is
   modelled as a normal distribution. It is about ±1 year for toddlers, ±4 years at 30 and ±8 at 70.
   A small "outlier" component means a mistagged face can't veto the rest. Multiplying the
   likelihoods of all faces gives a probability curve over the date. The suggestion is its median,
   together with a 90% range.
4. **Calibration** (optional, recommended) learns how wrong the model actually is on *your* photos.
   It uses digital photos with EXIF dates, plus scans you have already dated with this tool. It
   learns the bias per age group, the spread, and a correction per person, e.g. "the model always
   thinks Grandma is 5 years younger".

What precision to expect:

| Who is in the photo | Typical 90% range |
|---|---|
| A small child | about ±1 year |
| A school child or teenager | ±2–3 years |
| A single adult | ±6–10 years (still tells the 70s from the 90s) |
| Several family members of different generations | often ±1–2 years |

Photos without any known face can only be dated as part of an event. Select them together with
photos that have faces and tick *same event*.

## Workflow

1. Scan photos and upload them into an **Immich album**. There are two ways to organise this:
   - **One working album** (e.g. "Scans – to date") that you reuse for every batch. Tick
     *Remove photos from this album once their date is written*: dated photos leave the album
     but stay in Immich, so the album only ever holds what is left to do.
   - **One album per batch** (a box, an album, a film), which you can delete when you are done.
     Deleting an album in Immich never deletes its photos.
2. Wait for Immich's face detection, then name the people in Immich.
3. Open the tool, go to **People & birthdays**, and fill in the missing birthdays. They are saved
   in Immich.
4. Open the album in the tool:
   - Select photos whose date you know, type the date and click **Write date**. By default the
     photos keep their file order in the timeline (1 minute apart).
   - Select the rest (**Not dated yet**) and click **Estimate**. You can limit the range, for
     example "not after 1995" if the photos came from a box you know is older. Tick *same event*
     if the photos belong together.
   - Check the suggestions. Faces that don't fit the others are flagged ("check face/birthday").
     Accept them one by one, or select several and click **Write suggestions**.
5. Every so often, run **Calibration** on the start page. Each scan you date by hand makes the
   next estimates better.

Written photos get the tag `Age Guesser/Manual` or `Age Guesser/Estimated`, so you can find them
in Immich later. Estimated photos also get a line in their description, for example:
`Date estimated by Immich Age Guesser: 1987 (90% range 1985–1989; Anna ≈ 7 y, Bernd ≈ 36 y)`.
Immich has no field for "approximate date". A year is written as 2 July, the middle of the year;
this can be switched to 1 January in the settings.

## Installation

A ready-made Docker image is published at `ghcr.io/organoidschnitzel/immich-age-guesser`.
Everything else is set up in the browser.

### Option A: next to Immich, in Immich's own `docker-compose.yml` (recommended)

Add this service to the `services:` section of the `docker-compose.yml` you run Immich with:

```yaml
  immich-age-guesser:
    container_name: immich_age_guesser
    image: ghcr.io/organoidschnitzel/immich-age-guesser:latest
    ports:
      - "8080:8080"
    volumes:
      - ./age-guesser-data:/data
    restart: always
```

Then run `docker compose up -d` in that folder, open `http://<server>:8080` and fill in the
setup page. Use **`http://immich-server:2283`** as the Immich URL: both containers share Immich's
network, so the tool reaches Immich by its service name.

### Option B: on its own

Download [`docker-compose.yml`](docker-compose.yml) into an empty folder, run
`docker compose up -d`, and open `http://<server>:8080`. Use your server's address as the
Immich URL, e.g. `http://192.168.1.10:2283`.

### The setup page

The first visit opens the setup page:

1. In Immich, open *Account Settings → API Keys* and create a key. Either give it all permissions
   or these: `album.read`, `albumAsset.delete`, `asset.read`, `asset.view`, `asset.download`,
   `asset.update`, `face.read`, `person.read`, `person.update`, `tag.create`, `tag.asset`.
2. Paste the URL and the key, click **Test connection**, then **Save**.

You can change everything later under **Settings**. The settings, the tool's records and the
downloaded model (about 100 MB, fetched on the first estimate) live in the `/data` volume.

> The web UI has no login of its own and holds an API key that can change your photos. Only
> expose it on your LAN, or put it behind your reverse proxy's authentication.

**Updating:** `docker compose pull && docker compose up -d`.

### Without Docker (Python 3.10+)

```bash
python -m venv .venv && . .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[mivolo]"
immich-age-guesser install-mivolo         # MiVOLO's model code is not on PyPI
immich-age-guesser serve --port 8080      # then open http://localhost:8080
```

### CPU or GPU?

Use the **CPU of the home server**. Age estimation is a small model: roughly 0.1–0.3 s per face
on an i5-13500, so a batch of 500 scans takes minutes. It runs next to Immich and is always
available, with no dual-boot involved. A GPU is only worth it for tens of thousands of photos. To
use the RX 6800 anyway, build the image yourself with a ROCm PyTorch
(`docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/rocm6.2 .`), pass the GPU
into the container (`/dev/kfd`, `/dev/dri`) and set the device to `cuda`. PyTorch uses the same
name for ROCm.

## Command line

Everything the UI does is also available on the command line, e.g. with
`docker exec -it immich_age_guesser immich-age-guesser …`:

```bash
immich-age-guesser set-date    --album "Box 3" --date 1987 [--remove-from-album]
immich-age-guesser estimate    --album "Box 3" [--joint] [--not-before 1975] [--not-after 1995] [--apply [--remove-from-album]]
immich-age-guesser calibrate   [--per-person 60]
immich-age-guesser check       [--image face.jpg]   # tests the Immich connection and the model
immich-age-guesser check-model [--image face.jpg]   # tests only the model
```

`estimate` without `--apply` only stores suggestions. You can review them in the UI.

## Configuration

Normally everything is set on the **Settings** page and stored in `/data/config.json`.
Alternatively, any setting can be given as an environment variable (see
[`.env.example`](.env.example)). A setting given by an environment variable takes precedence and
is shown read-only on the Settings page.

| Variable | Default | |
|---|---|---|
| `IMMICH_URL`, `IMMICH_API_KEY` | – | Immich server and API key |
| `TIMEZONE` (or `TZ`) | `UTC` | time zone for written dates, e.g. `Europe/Berlin` |
| `DATE_ANCHOR` | `middle` | `middle` or `start` of a year/month |
| `REMOVE_FROM_ALBUM` | `false` | default for "remove photos from this album once dated" |
| `AGE_ESTIMATOR` | `mivolo` | or `ollama` (then `OLLAMA_URL`, `OLLAMA_MODEL`) |
| `TAG_ROOT` | `Age Guesser` | empty to disable tagging |
| `WRITE_DESCRIPTION` | `true` | add the explanation line to estimated photos |
| `DATA_DIR` | `/data` in Docker | where settings, records and the model are stored |

## Good to know

- **Better scans give better estimates.** Faces should be at least ~100 px. Scan prints at
  600 dpi or more, especially group photos.
- **Calibration and scans.** Scanners also write EXIF dates, but those record the scan date.
  Photos from known scanner models are ignored, and so are photos whose estimate is wildly off,
  which usually means a wrong date or a wrong face tag. Scans you dated by year, month or day with
  this tool do count as ground truth.
- **Age models have biases.** They can be off for faded black-and-white prints, strong makeup,
  costumes, and some ethnicities or age groups. Calibration corrects part of this, per person too.
- **Local data.** The tool keeps its settings and its own records in `DATA_DIR`: cached face
  ages, pending suggestions, and what was written. Deleting the folder loses no data in Immich.
- **MiVOLO license.** MiVOLO's code is Apache-2.0. The model weights are downloaded from
  [Hugging Face](https://huggingface.co/iitolstykh/mivolo_v2) under the license stated there;
  check it before using the tool for anything beyond private use.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run the whole pipeline against a fake Immich server (`tests/conftest.py`) and a fake age
model that reads ages from the test images. That covers cropping, inference, calibration, the
write-back, the setup page and the web API. In addition, CI builds the Docker image, starts it,
and runs `immich-age-guesser check-model`, which downloads the real MiVOLO model and runs it once.
