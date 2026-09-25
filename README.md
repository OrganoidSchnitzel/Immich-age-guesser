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

1. Scan photos and upload each batch (a box, an album, a film) into **its own Immich album**.
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
set `DATE_ANCHOR=start` for 1 January instead.

## Setup

### 1. Immich API key

In Immich, open *Account Settings → API Keys* and create a key with these permissions:

`album.read`, `asset.read`, `asset.view`, `asset.download`, `asset.update`, `face.read`,
`person.read`, `person.update`, `tag.create`, `tag.asset`

### 2. Run with Docker (recommended, e.g. on the home server)

```bash
cp .env.example .env              # set IMMICH_URL and IMMICH_API_KEY
cp docker-compose.example.yml docker-compose.yml
docker compose up -d --build
docker compose exec age-guesser immich-age-guesser check   # tests Immich + the model
```

Then open `http://<server>:8080`. The first estimate downloads the MiVOLO weights (~100 MB) into
`./data/hf-cache`.

> The web UI has no login of its own and holds an API key that can change your photos. Only
> expose it on your LAN, or put it behind your reverse proxy's authentication.

### Or run with plain Python (3.10+)

```bash
python -m venv .venv && . .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[mivolo]"
set -a; . ./.env; set +a
immich-age-guesser check
immich-age-guesser serve --port 8080
```

### CPU or GPU?

Use the **CPU of the home server**. Age estimation is a small model: roughly 0.1–0.3 s per face
on an i5-13500, so a batch of 500 scans takes minutes. It runs next to Immich and is always
available, with no dual-boot involved. A GPU is only worth it for tens of thousands of photos. To
use the RX 6800 anyway, build the image with a ROCm PyTorch
(`--build-arg TORCH_INDEX=https://download.pytorch.org/whl/rocm6.2`), pass the GPU into the
container (`/dev/kfd`, `/dev/dri`) and set `DEVICE=cuda`. PyTorch uses the same name for ROCm.

## Command line

Everything the UI does is also available on the command line:

```bash
immich-age-guesser set-date  --album "Box 3" --date 1987
immich-age-guesser estimate  --album "Box 3" [--joint] [--not-before 1975] [--not-after 1995] [--apply]
immich-age-guesser calibrate [--per-person 60]
immich-age-guesser check     [--image face.jpg]
```

`estimate` without `--apply` only stores suggestions. You can review them in the UI.

## Configuration

See [`.env.example`](.env.example). The most important settings:

| Variable | Default | |
|---|---|---|
| `IMMICH_URL`, `IMMICH_API_KEY` | – | required |
| `TIMEZONE` | `UTC` | time zone for written dates, e.g. `Europe/Berlin` |
| `DATE_ANCHOR` | `middle` | `middle` or `start` of a year/month |
| `AGE_ESTIMATOR` | `mivolo` | or `ollama` (then `OLLAMA_URL`, `OLLAMA_MODEL`) |
| `TAG_ROOT` | `Age Guesser` | empty to disable tagging |
| `WRITE_DESCRIPTION` | `true` | add the explanation line to estimated photos |

## Good to know

- **Better scans give better estimates.** Faces should be at least ~100 px. Scan prints at
  600 dpi or more, especially group photos.
- **Calibration and scans.** Scanners also write EXIF dates, but those record the scan date.
  Photos from known scanner models are ignored, and so are photos whose estimate is wildly off,
  which usually means a wrong date or a wrong face tag. Scans you dated by year, month or day with
  this tool do count as ground truth.
- **Age models have biases.** They can be off for faded black-and-white prints, strong makeup,
  costumes, and some ethnicities or age groups. Calibration corrects part of this, per person too.
- **Local data.** The tool keeps its own records in `DATA_DIR`: cached face ages, pending
  suggestions, and what was written. Deleting the folder loses no data in Immich.
- **MiVOLO license.** MiVOLO's code and weights have their own license. See the `license` folder
  of its repository before using it for anything beyond private use.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run the whole pipeline against a fake Immich server (`tests/conftest.py`) and a fake age
model that reads ages from the test images. That covers cropping, inference, calibration, the
write-back and the web API. The MiVOLO adapter itself is not covered by tests, so use
`immich-age-guesser check` to verify it on real hardware.
