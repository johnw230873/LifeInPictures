# Memorial Slideshow Creator

A simple Windows app that turns a folder of photos and a list of songs into an MP4 slideshow for a funeral or memorial service.

## Running it

Requires **Python 3.10 or newer** (Windows). `run.bat` installs the packages below and starts the app.

Double-click `run.bat`, or run:

```
pip install -r requirements.txt
python memorial_slideshow.py
```

`ffmpeg` is provided automatically by `imageio-ffmpeg`, so nothing else needs installing.
Downloaded music and temporary files are kept in a `slideshow_work` folder next to the video.

## Using it

1. **Photos folder**: choose the folder that holds the photos (JPG, PNG, BMP, GIF, TIFF, WEBP).
   - **Photo titles** (optional) are handwritten in pen-style ink. On a polaroid frame, the title is written on the white frame in blue-black ink. On other layouts, it's written centred just below the photo in white "gel pen" ink.
     - **Turn it off/on** with the **Photo titles** checkbox (on by default). When unticked, titles are ignored and the photos are shown without any writing.
     - A title can come from:
       - the photo's own **Title** (right-click the photo → Properties → Details → Title), or
       - a `captions.txt` file in the photos folder, with one `filename: title` per line, e.g. `IMG_0412.jpg: Mum at Piha Beach, 1972`. This file takes priority over the photo's own Title.
2. **Photo order**: sort by filename or by the date the photo was taken (EXIF).
3. **Songs**: enter one song per line in play order, e.g. `Time to Say Goodbye - Andrea Bocelli`. Each title is searched for online and downloaded. You can also paste a URL or use **Add local file…** to pick an audio file you already have.
4. Optional **title**, **subtitle** and **closing** cards, e.g. "In Loving Memory of …", "1942 – 2026", "Forever in our hearts".
   - **Opening photo** / **Closing photo** (optional): choose a photo to start and end the video with.
     - **With title or closing text:** the photo is shown on that card. A portrait photo goes beside the text and a wide photo goes above it.
     - **Without text:** the photo gets its own, slightly longer slide.
     - **Where it can come from:** the photo can be in the photos folder (it's then moved to the start or end, not shown twice) or anywhere else.
5. Pick the resolution, transition length (0.5–10 s) and style.
6. **Music length and speed** (optional):
   - **Analyse Music** downloads and measures the songs. It shows each song's length, the total at normal speed, and the number of photo slides.
   - **Auto** (on by default) plays the music up to 1.1× faster when there are only a few photos.
   - **Manual speed** turns Auto off and enables a slider from 0.80× (slower, longer video) to 1.20× (faster, shorter video). As you move it, the window shows the new video length, the difference from normal speed, and roughly how long each slide is shown. The pitch doesn't change.
7. Click **Create Video**.

## How the video is built

- Songs are downloaded to `slideshow_work\music` next to the output video, measured, and joined with 3-second crossfades. The audio fades in at the start, and the last song fades out over the final 10 seconds while the picture fades to black.
- If there are only a few photos for the amount of music (more than about 12 seconds per slide), the music is played faster, up to 1.1× normal speed, without changing the pitch. You can turn this off, or use **Manual speed** to choose the speed yourself.
- The video is exactly as long as the combined music. Each slide gets an equal share of that time. Slides with several photos get a little more time.
- If showing one photo per slide would give each slide less than about 5 seconds, photos are grouped 2–6 per slide. Two portrait photos next to each other are always shown side by side. Every photo is always shown.
- Transitions are slow crossfades, fades through black, or a mix of both. A transition never lasts more than 10 seconds, or more than 40% of a slide's time.
- Each slide has a slow zoom and pan, a soft blurred background, and photo borders with shadows.

Make sure you have the right to use any music you download.
