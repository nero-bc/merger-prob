import asyncio
import os
import time

from bot import (LOGGER, UPLOAD_AS_DOC, UPLOAD_TO_DRIVE, delete_all, formatDB,
                 gDict, queueDB)
from config import Config
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from helpers.display_progress import Progress
from helpers.ffmpeg_helper import MergeSub, MergeVideo, take_screen_shot
from helpers.rclone_upload import rclone_driver, rclone_upload
from helpers.uploader import uploadVideo
from helpers.utils import UserSettings
from PIL import Image
from pyrogram import Client
from pyrogram.errors import MessageNotModified
from pyrogram.errors.rpc_error import UnknownError
from pyrogram.types import CallbackQuery


async def mergeNow(c: Client, cb: CallbackQuery, new_file_name: str):
    # Get the original message
    omess = cb.message.reply_to_message
    vid_list = list()
    sub_list = list()
    sIndex = 0
    # Update the status message
    await cb.message.edit("**Processing**")
    duration = 0
    # Get the list of video and subtitle message IDs from the queue
    list_message_ids = queueDB.get(cb.from_user.id)["videos"]
    list_message_ids.sort()
    list_subtitle_ids = queueDB.get(cb.from_user.id)["subtitles"]
    LOGGER.info(Config.IS_PREMIUM)
    LOGGER.info(f"Videos: {list_message_ids}")
    LOGGER.info(f"Subs: {list_subtitle_ids}")
    # Check if the video queue is empty
    if list_message_ids is None:
        await cb.answer("**Queue Empty**", show_alert=True)
        await cb.message.delete(True)
        return
    # Create a directory to store downloaded files
    if not os.path.exists(f"downloads/{str(cb.from_user.id)}/"):
        os.makedirs(f"downloads/{str(cb.from_user.id)}/")
    input_ = f"downloads/{str(cb.from_user.id)}/input.txt"
    all = len(list_message_ids)
    n = 1
    # Iterate through each video message and download the file
    for i in await c.get_messages(chat_id=cb.from_user.id, message_ids=list_message_ids):
        media = i.video or i.document
        await cb.message.edit(f"**Starting Download Of ... `{media.file_name}`**")
        LOGGER.info(f"**Starting Download Of ... {media.file_name}**")
        await asyncio.sleep(5)
        file_dl_path = None
        sub_dl_path = None
        try:
            c_time = time.time()
            # Display progress while downloading
            prog = Progress(cb.from_user.id, c, cb.message)
            file_dl_path = await c.download_media(
                message=media,
                file_name=f"downloads/{str(cb.from_user.id)}/{str(i.id)}/vid.mkv",
                progress=prog.progress_for_pyrogram,
                progress_args=(f"**Downloading:\n{media.file_name}**", c_time, f"**Downloading: {n}/{all}**"),
            )
            n += 1
            # Check if the download process is interrupted
            if gDict[cb.message.chat.id] and cb.message.id in gDict[cb.message.chat.id]:
                return
            await cb.message.edit(f"**Downloaded Successfully...\n{media.file_name}**")
            LOGGER.info(f"**Downloaded Successfully...\n{media.file_name}**")
            await asyncio.sleep(5)
        except UnknownError as e:
            LOGGER.info(e)
            pass
        except Exception as downloadErr:
            LOGGER.info(f"**Failed To Download Error: {downloadErr}**")
            # Remove the failed video from the queue
            queueDB.get(cb.from_user.id)["video"].remove(i.id)
            await cb.message.edit("**File Skipped!**")
            await asyncio.sleep(4)
            continue

        # Download and merge subtitles if available
        if list_subtitle_ids[sIndex] is not None:
            a = await c.get_messages(
                chat_id=cb.from_user.id, message_ids=list_subtitle_ids[sIndex]
            )
            sub_dl_path = await c.download_media(
                message=a,
                file_name=f"downloads/{str(cb.from_user.id)}/{str(a.id)}/",
            )
            LOGGER.info("**Got Sub:**", a.document.file_name)
            file_dl_path = await MergeSub(file_dl_path, sub_dl_path, cb.from_user.id)
            LOGGER.info("**Added Subs**")
        sIndex += 1

        # Extract video metadata
        metadata = extractMetadata(createParser(file_dl_path))
        try:
            if metadata.has("duration"):
                duration += metadata.get("duration").seconds
            vid_list.append(f"file '{file_dl_path}'")
        except:
            # Handle corrupted video file
            await delete_all(root=f"downloads/{str(cb.from_user.id)}")
            queueDB.update(
                {cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}}
            )
            formatDB.update({cb.from_user.id: None})
            await cb.message.edit("**Video Is Corrupted**")
            return

    # Remove duplicate video entries
    _cache = list()
    for i in range(len(vid_list)):
        if vid_list[i] not in _cache:
            _cache.append(vid_list[i])
    vid_list = _cache

    LOGGER.info(f"**Trying to merge videos\nUserID {cb.from_user.id}**")
    await cb.message.edit("**Trying To Merge Videos...**")
    with open(input_, "w") as _list:
        _list.write("\n".join(vid_list))
    # Merge the videos into a single file
    merged_video_path = await MergeVideo(
        input_file=input_, user_id=cb.from_user.id, message=cb.message, format_="mkv"
    )
    # Handle failed video merging
    if merged_video_path is None:
        await cb.message.edit("**Failed To Merge Video !**")
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return

    try:
        await cb.message.edit("**Successfully Merged Video !**")
    except MessageNotModified:
        await cb.message.edit("**Successfully Merged Video !**")

    LOGGER.info(f"**Video merged for: {cb.from_user.first_name}** ")
    await asyncio.sleep(3)
    file_size = os.path.getsize(merged_video_path)
    os.rename(merged_video_path, new_file_name)
    await cb.message.edit(
        f"**Renamed Merged Video To\n {new_file_name.rsplit('/',1)[-1]}**"
    )
    await asyncio.sleep(3)
    merged_video_path = new_file_name

    # Upload the merged video to Google Drive if configured
    if UPLOAD_TO_DRIVE[f"{cb.from_user.id}"]:
        await rclone_driver(omess, cb, merged_video_path)
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return

    # Handle file size restrictions for non-premium users
    if file_size > 2044723200 and Config.IS_PREMIUM == False:
        await cb.message.edit(
            f"**Video Is Larger Than 2GB, Can't Upload,\nTell {Config.OWNER_USERNAME} To Add Premium Account For 4GB TG Uploads**"
        )
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return

    # Handle file size restrictions for premium users
    if Config.IS_PREMIUM and file_size > 4241280205:
        await cb.message.edit(
            f"**Video Is Larger Than 4GB, Can't Upload,\nTell {Config.OWNER_USERNAME} To Die With Premium Account**"
        )
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return

    await cb.message.edit("**Extracting Video Data...**")
    duration = 1
    try:
        # Extract merged video duration
        metadata = extractMetadata(createParser(merged_video_path))
        if metadata.has("duration"):
            duration = metadata.get("duration").seconds
    except Exception as er:
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        await cb.message.edit("**Merged Video Is Corrupted**")
        return

    try:
        # Download and set video thumbnail
        user = UserSettings(cb.from_user.id, cb.from_user.first_name)
        thumb_id = user.thumbnail
        if thumb_id is None:
            raise Exception
        video_thumbnail = f"downloads/{str(cb.from_user.id)}_thumb.jpg"
        await c.download_media(message=str(thumb_id), file_name=video_thumbnail)
    except Exception as err:
        LOGGER.info("**Generating thumb**")
        # Generate thumbnail if not provided
        video_thumbnail = await take_screen_shot(
            merged_video_path, f"downloads/{str(cb.from_user.id)}", (duration / 2)
        )

    # Resize thumbnail if needed
    width = 1280
    height = 720
    try:
        thumb = extractMetadata(createParser(video_thumbnail))
        height = thumb.get("height")
        width = thumb.get("width")
        img = Image.open(video_thumbnail)
        if width > height:
            img.resize((320, height))
        elif height > width:
            img.resize((width, 320))
        img.save(video_thumbnail)
        Image.open(video_thumbnail).convert("RGB").save(video_thumbnail, "JPEG")
    except:
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        await cb.message.edit("**Merged Video Is Corrupted**")
        return

    # Upload the final merged video to Telegram
    await uploadVideo(
        c=c,
        cb=cb,
        merged_video_path=merged_video_path,
        width=width,
        height=height,
        duration=duration,
        video_thumbnail=video_thumbnail,
        file_size=os.path.getsize(merged_video_path),
        upload_mode=UPLOAD_AS_DOC[f"{cb.from_user.id}"],
    )

    # Delete temporary files and update the queue
    await cb.message.delete(True)
    await delete_all(root=f"downloads/{str(cb.from_user.id)}")
    queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
    formatDB.update({cb.from_user.id: None})
    return
