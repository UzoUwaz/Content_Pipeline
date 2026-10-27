# YouTube image pipeline

Generates one MS Paint style image per beat of a timestamped script.

## Setup (once)

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and paste your OpenAI API key into it.

## Each video

1. Transcribe in Colab with Whisper, download the `.srt`
2. Drop the `.srt` into `input/`
3. Preview the beat breakdown — free:

   ```
   python generate_images.py --dry-run
   ```

4. Too many images? Raise the merge threshold:

   ```
   python generate_images.py --dry-run --min-seconds 14
   ```

5. Test three real images:

   ```
   python generate_images.py --limit 3
   ```

6. Run the batch:

   ```
   python generate_images.py
   ```

Images land in `output/<srt name>/` alongside a `manifest.tsv`
mapping each timestamp to its file and its line of script.

## Notes

- Re-running skips images that already exist, so an interrupted
  run resumes where it stopped.
- To regenerate one image, delete that PNG and re-run.
- Edit `prompts/style_a.txt` to change the look. No code changes needed.
- `--quality medium` or `--quality high` costs more per image.
