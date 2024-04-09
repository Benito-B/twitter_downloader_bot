import html
import json
import logging
import traceback
from io import StringIO
from os import getenv, makedirs
from typing import Optional, TypeAlias, Literal
from urllib.parse import urlsplit
from uuid import uuid4
import requests

try:
    import re2 as re
except ImportError:
    import re
import telegram.error
from telegram import Update, InputTextMessageContent, InlineQueryResultArticle, \
    InlineQueryResultPhoto, InlineQueryResultGif, InlineQueryResultVideo, InlineQueryResult, InputMediaDocument, \
    LinkPreviewOptions, ReplyParameters
from telegram.ext import CommandHandler, CallbackContext, Application, InlineQueryHandler, ContextTypes, \
    PicklePersistence, filters

ContextCounters: TypeAlias = Literal["commands_handled", "messages_handled", "media_downloaded"]
CORRECT_TWITTER_PATTERN = r"http(?:s)?:\/\/(?:www)?(twitter|x)\.com\/([a-zA-Z0-9_]+)/(status|web)/\d+"

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
# Grab env values
BOT_TOKEN = getenv('BOT_TOKEN')
DEVELOPER_ID = getenv('DEVELOPER_ID')


def extract_tweet_ids(update: Update, text: str) -> Optional[list[str]]:
    """Extract tweet IDs from message."""

    # For t.co links
    unshortened_links = ''
    for link in re.findall(r"t\.co/[a-zA-Z0-9]+", text):
        try:
            unshortened_link = requests.get('https://' + link).url
            unshortened_links += '\n' + unshortened_link
            log_handling(update, 'info', f'Unshortened t.co link [https://{link} -> {unshortened_link}]')
        except:
            log_handling(update, 'info', f'Could not unshorten link [https://{link}]')

    # Parse IDs from received text
    tweet_ids = re.findall(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", text + unshortened_links)
    tweet_ids = list(dict.fromkeys(tweet_ids))
    return tweet_ids or None


def scrape_media(tweet_id: int) -> list[dict]:
    r = requests.get(f'https://api.vxtwitter.com/Twitter/status/{tweet_id}')
    r.raise_for_status()
    return r.json()['media_extended']


def get_media(tweet_media: list) -> tuple[list, list, list]:
    photos = [media for media in tweet_media if media["type"] == "image"]
    gifs = [media for media in tweet_media if media["type"] == "gif"]
    videos = [media for media in tweet_media if media["type"] == "video"]
    return photos, gifs, videos


def get_media_for_inline(update: Update, context: CallbackContext, tweet_media: list) -> list[InlineQueryResult]:
    photos, gifs, videos = get_media(tweet_media)
    media_group: list[InlineQueryResult] = []
    if photos:
        media_group += get_photos(update, context, photos)
    elif gifs:
        media_group += get_gifs(update, context, gifs)
    elif videos:
        media_group += get_videos(update, context, videos)
    return media_group


def get_photos(update: Update, context: CallbackContext, twitter_photos: list[dict]) -> list[InlineQueryResultPhoto]:
    """Reply with photo group."""
    photo_group = []
    for photo in twitter_photos:
        photo_url = photo['url']
        log_handling(update, 'info', f'Photo[{len(photo)}] url: {photo_url}')
        final_url = get_photo_url(update, photo_url)
        photo_group.append(InlineQueryResultPhoto(
            id=str(uuid4()),
            photo_url=final_url,
            thumbnail_url=final_url
        ))
        increase_context_counter(context, "media_downloaded")
    return photo_group


def get_gifs(update: Update, context: CallbackContext, twitter_gifs: list[dict]) -> list[InlineQueryResultGif]:
    """Reply with GIF animations."""
    gif_group = []
    for gif in twitter_gifs:
        gif_url = gif['url']
        log_handling(update, 'info', f'Gif url: {gif_url}')
        gif_group.append(InlineQueryResultGif(
            id=str(uuid4()),
            thumbnail_url=str(gif['thumbnail_url']),
            gif_url=str(gif_url)
        ))
        increase_context_counter(context, "media_downloaded")
    return gif_group


def get_videos(update: Update, context: CallbackContext, twitter_videos: list[dict]) -> \
        list[InlineQueryResultVideo]:
    """Reply with videos."""
    video_group = []
    for video in twitter_videos:
        video_url = video['url']
        try:
            request = requests.get(video_url, stream=True)
            request.raise_for_status()
            if (video_size := int(request.headers['Content-Length'])) <= 20 * 1024 * 1024:
                video_group.append(InlineQueryResultVideo(
                    id=str(uuid4()),
                    thumbnail_url=video['thumbnail_url'],
                    video_url=video_url,
                    title='Video',
                    mime_type='video/mp4'
                ))
            else:
                log_handling(update, 'info', f'Video size ({video_size}) is too big')
                video_group.append(InlineQueryResultArticle(
                    id=str(uuid4()),
                    title='Video too big!',
                    input_message_content=InputTextMessageContent('Video too big')
                ))
        except (requests.HTTPError, KeyError, telegram.error.BadRequest, requests.exceptions.ConnectionError) as exc:
            log_handling(update, 'info', f'{exc.__class__.__qualname__}: {exc}')
            log_handling(update, 'info', 'Error occurred when trying to send video, sending direct link')
            update.effective_message.reply_text(f'Error occurred when trying to send video. Direct link:\n'
                                                f'{video_url}', quote=True)
        print('data:', context.bot_data)
        increase_context_counter(context, "media_downloaded")
    return video_group


async def grab_command(update: Update, context: CallbackContext) -> None:
    url = context.args[0]
    if re.match(CORRECT_TWITTER_PATTERN, url):
        tweet_ids = extract_tweet_ids(update, url)
        if tweet_ids:
            await update.effective_message.reply_text(f'tweet: {url}', disable_web_page_preview=True)
        for tweet_id in tweet_ids:
            media = scrape_media(tweet_id)
            photos, gifs, videos = get_media(media)
            if photos:
                await command_send_photos(context, photos, update)
            if gifs:
                await command_send_gifs(context, gifs, update)
            if videos:
                await command_send_videos(context, videos, update)
        await update.effective_message.delete()
    else:
        await update.effective_message.reply_text("That's not a valid twitter URL or I couldn't find any media on it")
    increase_context_counter(context, "commands_handled")


async def donate_command(update: Update, context: CallbackContext) -> None:
    await update.effective_message.reply_text("If you like the bot and want to support me, please buy me a coffee!" +
                                              'https://www.buymeacoffee.com/benitob')


async def command_send_videos(context: CallbackContext, videos: list, update: Update) -> None:
    for video in videos:
        video_url = video['url']
        try:
            request = requests.get(video_url, stream=True)
            request.raise_for_status()
            if (int(request.headers['Content-Length'])) <= 20 * 1024 * 1024:
                await update.effective_message.reply_video(video=video_url, quote=False)
            else:
                log_handling(update, 'info', 'Video is too large, sending direct link')
                await update.effective_message.reply_text(f'Video is too large for Telegram upload. Link:\n'
                                                          f'{video_url}', quote=True)
        except (requests.HTTPError, KeyError, telegram.error.BadRequest, requests.exceptions.ConnectionError) as exc:
            log_handling(update, 'info', f'{exc.__class__.__qualname__}: {exc}')
            log_handling(update, 'info', 'Error occurred when trying to send video, sending direct link')
            await update.effective_message.reply_text(f'Error occurred when trying to send video. Direct link:\n'
                                                      f'{video_url}', quote=True)
        increase_context_counter(context, "media_downloaded")


async def command_send_gifs(context: CallbackContext, gifs: list, update: Update) -> None:
    for gif in gifs:
        gif_url = gif['url']
        log_handling(update, 'info', f'Gif url: {gif_url}')
        increase_context_counter(context, "media_downloaded")
        await update.effective_message.reply_animation(animation=gif_url, quote=False)


async def command_send_photos(context: CallbackContext, photos: list, update: Update) -> None:
    photo_group = []
    for p in photos:
        photo_url = p['url']
        photo_group.append(InputMediaDocument(media=get_photo_url(update, photo_url)))
        increase_context_counter(context, "media_downloaded")
    await update.effective_message.reply_media_group(photo_group, quote=False)


def get_photo_url(update, url) -> str:
    # Try changing requested quality to 'orig'
    parsed_url = urlsplit(url)
    try:
        new_url = parsed_url._replace(query='format=jpg&name=orig').geturl()
        log_handling(update, 'info', 'New photo url: ' + new_url)
        requests.head(new_url).raise_for_status()
        return new_url
    except requests.HTTPError:
        log_handling(update, 'info', 'orig quality not available, using original url')
        return url


def increase_context_counter(context: CallbackContext, counter_name: ContextCounters) -> None:
    if 'stats' not in context.bot_data:
        init_stats(context)
    context.bot_data['stats'][counter_name] += 1


def init_stats(context: CallbackContext) -> None:
    context.bot_data['stats'] = {
        'media_downloaded': 0,
        'messages_handled': 0,
        'commands_handled': 0
    }


def log_handling(update: Update, level: str, message: str) -> None:
    """Log message with chat_id and message_id."""
    _level = getattr(logging, level.upper())
    logger.log(_level, f'[{update.effective_user.id}] {message}')


async def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""

    if isinstance(context.error, telegram.error.Forbidden):
        return

    if isinstance(context.error, telegram.error.Conflict):
        logger.error("Telegram requests conflict")
        return

    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # if there is no update, don't send an error report (probably a network error, happens sometimes)
    if update is None:
        return

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'#error_report\n'
        f'An exception was raised in runtime\n'
        f'update = {json.dumps(update_str, indent=2, ensure_ascii=False)}'
        '\n\n'
        f'context.chat_data = {str(context.chat_data)}\n\n'
        f'context.user_data = {str(context.user_data)}\n\n'
        f'{tb_string}'
    )

    # Finally, send the message
    error_class_name = ".".join([context.error.__class__.__module__, context.error.__class__.__qualname__])
    await context.bot.send_document(chat_id=DEVELOPER_ID, document=message.encode(), filename='error_report.txt',
                                    caption='#error_report\nAn exception was raised:\n' +
                                            f'{error_class_name}: {str(context.error)}')


async def stats_command(update: Update, context: CallbackContext) -> None:
    """Send stats when the command /stats is issued."""
    print('stats in')
    if 'stats' not in context.bot_data:
        init_stats(context)
    logger.info(f'Sent stats: {context.bot_data["stats"]}')
    await update.effective_message.reply_markdown_v2(
        f'*Bot stats:*\nMessages handled: *{context.bot_data["stats"].get("messages_handled")}*' +
        f'\nCommands handled: *{context.bot_data["stats"].get("commands_handled")}*' +
        f'\nMedia downloaded: *{context.bot_data["stats"].get("media_downloaded")}*')


async def reset_stats_command(update: Update, context: CallbackContext) -> None:
    """Reset stats when the command /resetstats is issued."""
    init_stats(context)
    logger.info("Bot stats have been reset")
    await update.effective_message.reply_text("Bot stats have been reset")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    log_handling(update, 'info', f'Received /start command from userId {update.effective_user.id}')
    user = update.effective_user
    await update.effective_message.reply_markdown_v2(
        fr'Hi {user.mention_markdown_v2()}\!' +
        '\n' +
        fr'Just @ me with a twitter link and I\'ll try and send you the media'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline query. This is run when you type: @botusername <query>"""
    query = update.inline_query.query

    if not query:  # empty query should not be handled
        return

    tweet_ids = extract_tweet_ids(update, query)
    results = []
    for tweet_id in tweet_ids:
        try:
            media = scrape_media(tweet_id)
            if media:
                log_handling(update, 'info', f'tweet media: {media}')
                results = get_media_for_inline(update, context, media)
            else:
                log_handling(update, 'info', f'Tweet {tweet_id} has no media')
        except Exception:
            log_handling(update, 'error', f'Error occurred when scraping tweet {tweet_id}: {traceback.format_exc()}')
    if len(results) == 0:
        results.append(InlineQueryResultArticle(
            id=str(uuid4()),
            title="There's nothing I can get for you there!",
            input_message_content=InputTextMessageContent("There's nothing I can get for you there!"),
        ))
    increase_context_counter(context, "messages_handled")
    await update.inline_query.answer(results)


def main() -> None:
    """Run the bot."""
    # Create the Application and pass it bot token and persistence
    makedirs('data', exist_ok=True)
    persistence = PicklePersistence(filepath="data/persistence")
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("grab", grab_command, has_args=True))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command, filters.Chat(int(DEVELOPER_ID))))
    application.add_handler(CommandHandler("resetstats", reset_stats_command, filters.Chat(int(DEVELOPER_ID))))
    application.add_handler(CommandHandler("donate", donate_command))

    # on inline queries - show corresponding inline results
    application.add_handler(InlineQueryHandler(inline_query, CORRECT_TWITTER_PATTERN))

    # Register error handler
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
