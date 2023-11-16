import asyncio
import os
import time

# Importing necessary modules and functions from your bot
from bot import (
    LOGGER,
    SUBTITLE_EXTENSIONS,
    UPLOAD_AS_DOC,
    UPLOAD_TO_DRIVE,
    VIDEO_EXTENSIONS,
    delete_all,
    formatDB,
    gDict,
    queueDB,
)
from config import Config
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from helpers.display_progress import Progress
from helpers.ffmpeg_helper import MergeSubNew, take_screen_shot
from helpers.rclone_upload import rclone_driver, rclone_upload
from helpers.uploader import uploadVideo
from helpers.utils import UserSettings
from PIL import Image
from pyrogram import Client
from pyrogram.errors import MessageNotModified
from pyrogram.errors.exceptions.flood_420 import FloodWait
from pyrogram.errors.rpc_error import UnknownError
from pyrogram.types import CallbackQuery, Message


async def mergeSub(c: Client, cb: CallbackQuery, new_file_name: str):
    omess = cb.message.reply_to_message
    vid_list = list()
    # Update the status message
    await cb.message.edit("**Processing...**")
    duration = 0
    video_mess = queueDB.get(cb.from_user.id)["videos"][0]
    list_message_ids: list = queueDB.get(cb.from_user.id)["subtitles"]
    list_message_ids.insert(0, video_mess)
    list_message_ids.sort()

    # Check if the subtitle queue is empty
    if list_message_ids is None:
        await cb.answer("**Queue Empty**", show_alert=True)
        await cb.message.delete(True)
        return

    # Create a directory to store downloaded files
    if not os.path.exists(f"downloads/{str(cb.from_user.id)}/"):
        os.makedirs(f"downloads/{str(cb.from_user.id)}/")

    msgs: list[Message] = await c.get_messages(
        chat_id=cb.from_user.id, message_ids=list_message_ids
    )
    all = len(msgs)
    n = 1
    # Iterate through each subtitle message and download the file
    for i in msgs:
        media = i.video or i.document
        await cb.message.edit(f"**Starting Download Of...\n{media.file_name}**")
        LOGGER.info(f"**Starting Download Of...\n{media.file_name}**")

        # Determine the file name based on the extension
        currentFileNameExt = media.file_name.rsplit(sep=".")[-1].lower()
        if currentFileNameExt in VIDEO_EXTENSIONS:
            tmpFileName = "vid.mkv"
        elif currentFileNameExt in SUBTITLE_EXTENSIONS:
            tmpFileName = "sub." + currentFileNameExt

        await asyncio.sleep(5)
        file_dl_path = None
        try:
            c_time = time.time()
            # Display progress while downloading
            prog = Progress(cb.from_user.id, c, cb.message)
            file_dl_path = await c.download_media(
                message=media,
                file_name=f"downloads/{str(cb.from_user.id)}/{str(i.id)}/{tmpFileName}",
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
        except Exception as downloadErr:
            LOGGER.warning(f"**Failed To Download Error:\n{downloadErr}**")
            # Remove the failed subtitle from the queue
            queueDB.get(cb.from_user.id)["subtitles"].remove(i.id)
            await cb.message.edit("**File Skipped!**")
            await asyncio.sleep(4)
            await cb.message.delete(True)
            continue
        vid_list.append(f"{file_dl_path}")

    # Merge the subtitles with the video
    subbed_video = MergeSubNew(
        filePath=vid_list[0],
        subPath=vid_list[1],
        user_id=cb.from_user.id,
        file_list=vid_list,
    )

    # Handle failed subtitle merging
    if subbed_video is None:
        await cb.message.edit("**Failed To Add Subs Video !**")
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return

    try:
        await cb.message.edit("**Successfully Muxed Video !**")
    except MessageNotModified:
        await cb.message.edit("**Successfully Muxed Video !**")
    LOGGER.info(f"**Video muxed for: {cb.from_user.first_name}** ")
    await asyncio.sleep(3)
    file_size = os.path.getsize(subbed_video)
    os.rename(subbed_video, new_file_name)
    await cb.message.edit(
        f"**Renaming Video To\n {new_file_name.rsplit('/',1)[-1]}**"
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
            f"**Video Is Larger Than 2GB Can't Upload,\n\nTell {Config.OWNER_USERNAME} To Add Premium Account For 4GB TG Uploads**"
        )
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return
    if Config.IS_PREMIUM and file_size > 4241280205:
        await cb.message.edit(
            "**Video Is Larger Than 4GB Can't Upload,\n\nTell {Config.OWNER_USERNAME} To Die With Premium Account**"
        )
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        return
    await cb.message.edit("**Extracting Video Data...**")

    duration = 1
    try:
        metadata = extractMetadata(createParser(merged_video_path))
        if metadata.has("duration"):
            duration = metadata.get("duration").seconds
    except Exception as er:
        await delete_all(root=f"downloads/{str(cb.from_user.id)}")
        queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
        formatDB.update({cb.from_user.id: None})
        await cb.message.edit("**Merged Video Is Corrupted**")
        return

    # Download or generate video thumbnail
    try:
        user = UserSettings(cb.from_user.id, cb.from_user.first_name)
        thumb_id = user.thumbnail
        if thumb_id is None:
            raise Exception
        video_thumbnail = f"downloads/{str(cb.from_user.id)}_thumb.jpg"
        await c.download_media(message=str(thumb_id), file_name=video_thumbnail)
    except Exception as err:
        LOGGER.info("**Generating thumb**")
        video_thumbnail = await take_screen_shot(
            merged_video_path, f"downloads/{str(cb.from_user.id)}", (duration / 2)
        )

    # Set default width and height
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
        await cb.message.edit(
            "**Merged Video Is Corrupted**\n\n**Try Setting Custom Thumbnail**",
        )
        return

    # Upload the video to Telegram
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

    # Delete temporary files and reset the user's queue
    await cb.message.delete(True)
    await delete_all(root=f"downloads/{str(cb.from_user.id)}")
    queueDB.update({cb.from_user.id: {"videos": [], "subtitles": [], "audios": []}})
    formatDB.update({cb.from_user.id: None})
    return
