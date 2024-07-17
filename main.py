import os
import asyncio
import logging
from notion_client import AsyncClient
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from deepl import Translator

# Configuration des logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialiser les tokens API
NOTION_API_TOKEN = os.environ.get('NOTION_API_TOKEN')
DEEPL_API_TOKEN = os.environ.get('DEEPL_API_TOKEN')

if not NOTION_API_TOKEN or not DEEPL_API_TOKEN:
    logger.error("Les tokens API sont manquants. Assurez-vous de définir NOTION_API_TOKEN et DEEPL_API_TOKEN.")
    sys.exit(1)

# Initialiser les clients API
notion = AsyncClient(auth=NOTION_API_TOKEN)
translator = Translator(DEEPL_API_TOKEN)

# Créez un ThreadPoolExecutor global
thread_pool = ThreadPoolExecutor(max_workers=5)

# Liste des langues supportées par DeepL
SUPPORTED_LANGUAGES = {
    'source': ['bg', 'cs', 'da', 'de', 'el', 'en', 'es', 'et', 'fi', 'fr', 'hu', 'id', 'it', 'ja', 'lt', 'lv', 'nl', 'pl', 'pt', 'ro', 'ru', 'sk', 'sl', 'sv', 'tr', 'zh'],
    'target': ['bg', 'cs', 'da', 'de', 'el', 'en-gb', 'en-us', 'es', 'et', 'fi', 'fr', 'hu', 'id', 'it', 'ja', 'lt', 'lv', 'nl', 'pl', 'pt-pt', 'pt-br', 'ro', 'ru', 'sk', 'sl', 'sv', 'tr', 'zh']
}

def extract_database_id(url):
    """Extrait l'ID de la base de données à partir de l'URL Notion."""
    match = re.search(r'([a-f0-9]{32})', url)
    if match:
        return match.group(1)
    raise ValueError("URL Notion invalide. Assurez-vous qu'elle contient un ID de base de données valide.")

async def get_all_pages(database_id):
    """Obtient toutes les pages d'une base de données."""
    try:
        pages = []
        has_more = True
        start_cursor = None

        while has_more:
            response = await notion.databases.query(
                database_id=database_id,
                start_cursor=start_cursor
            )
            pages.extend(response["results"])
            has_more = response["has_more"]
            start_cursor = response["next_cursor"]

        logger.info(f"Nombre total de pages trouvées : {len(pages)}")
        return pages
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des pages : {str(e)}")
        raise

async def get_pages_to_translate(database_id):
    """Obtient toutes les pages avec le statut 'A traduire' d'une base de données."""
    try:
        all_pages = await get_all_pages(database_id)
        pages_to_translate = [
            page for page in all_pages
            if page['properties'].get('Statut', {}).get('status', {}).get('name') == 'A traduire'
        ]
        logger.info(f"Nombre de pages à traduire trouvées : {len(pages_to_translate)}")
        return pages_to_translate
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des pages à traduire : {str(e)}")
        raise

async def translate_text(text, from_lang, to_lang):
    """Traduit un texte en utilisant l'API DeepL de manière asynchrone."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            thread_pool,
            lambda: translator.translate_text(text, source_lang=from_lang, target_lang=to_lang)
        )
        return result.text
    except Exception as e:
        logger.error(f"Erreur lors de la traduction du texte : {str(e)}")
        raise

async def translate_block(block, from_lang, to_lang):
    """Traduit un bloc Notion."""
    try:
        if block['type'] in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'toggle', 'quote', 'callout']:
            text_elements = block[block['type']]['rich_text']
            for text_element in text_elements:
                if text_element['type'] == 'text':
                    original_text = text_element['text']['content']
                    translated_text = await translate_text(original_text, from_lang, to_lang)
                    text_element['text']['content'] = translated_text
        return block
    except Exception as e:
        logger.error(f"Erreur lors de la traduction du bloc {block['id']}: {str(e)}")
        raise

async def translate_page(page_id, from_lang, to_lang):
    """Traduit directement une page Notion."""
    try:
        # Traduire les propriétés de la page
        page = await notion.pages.retrieve(page_id=page_id)
        updated_properties = {}
        for prop_name, prop_value in page['properties'].items():
            if prop_value['type'] == 'title':
                if prop_value['title']:
                    original_text = prop_value['title'][0]['plain_text']
                    translated_text = await translate_text(original_text, from_lang, to_lang)
                    updated_properties[prop_name] = {
                        'title': [{
                            'text': {
                                'content': translated_text
                            }
                        }]
                    }
            elif prop_value['type'] == 'rich_text':
                if prop_value['rich_text']:
                    original_text = ''.join([text['plain_text'] for text in prop_value['rich_text']])
                    translated_text = await translate_text(original_text, from_lang, to_lang)
                    updated_properties[prop_name] = {
                        'rich_text': [{
                            'text': {
                                'content': translated_text
                            }
                        }]
                    }
            # Ajoutez d'autres types de propriétés si nécessaire

        if updated_properties:
            await notion.pages.update(page_id=page_id, properties=updated_properties)
            logger.info(f"Propriétés de la page traduites avec succès: {page_id}")
        else:
            logger.warning(f"Aucune propriété modifiable trouvée pour la page {page_id}.")

        # Traduire les blocs de la page
        blocks = await notion.blocks.children.list(block_id=page_id)
        for block in blocks['results']:
            translated_block = await translate_block(block, from_lang, to_lang)
            await notion.blocks.update(block_id=block['id'], **translated_block)

        # Ajouter une propriété pour indiquer la langue de la page
        await notion.pages.update(
            page_id=page_id,
            properties={
                "Langue": {"select": {"name": to_lang}},
                "Statut": {"status": {"name": "Traduit"}}
            }
        )

    except Exception as e:
        logger.error(f"Erreur lors de la traduction de la page {page_id}: {str(e)}")
        raise

async def translate_all_pages_to_translate(database_id, from_lang, to_lang):
    """Traduit toutes les pages avec le statut 'A traduire' d'une base de données."""
    try:
        pages = await get_pages_to_translate(database_id)
        for page in pages:
            await translate_page(page['id'], from_lang, to_lang)
    except Exception as e:
        logger.error(f"Erreur lors de la traduction des pages : {str(e)}")
        raise

def get_user_input():
    """Obtient les entrées de l'utilisateur pour l'URL de la base de données et les langues de traduction."""
    default_url = "https://www.notion.so/decarbonaction/bcf85bf467ab4623960cb0074962df34?v=8855845a215a46009624fe8a8ec336c5"
    default_from_lang = 'fr'
    default_to_lang = 'nl'

    url = input(f"Entrez l'URL de la base de données Notion (appuyez sur Entrée pour utiliser {default_url}): ").strip()
    if not url:
        url = default_url

    from_lang = input(f"Entrez la langue source {SUPPORTED_LANGUAGES['source']} (appuyez sur Entrée pour utiliser {default_from_lang}): ").strip().lower()
    if not from_lang:
        from_lang = default_from_lang
    while from_lang not in SUPPORTED_LANGUAGES['source'] and from_lang != 'auto':
        print("Langue source non valide. Veuillez réessayer.")
        from_lang = input(f"Entrez la langue source {SUPPORTED_LANGUAGES['source']} (appuyez sur Entrée pour utiliser {default_from_lang}): ").strip().lower()
        if not from_lang:
            from_lang = default_from_lang

    to_lang = input(f"Entrez la langue cible {SUPPORTED_LANGUAGES['target']} (appuyez sur Entrée pour utiliser {default_to_lang}): ").strip().lower()
    if not to_lang:
        to_lang = default_to_lang
    while to_lang not in SUPPORTED_LANGUAGES['target']:
        print("Langue cible non valide. Veuillez réessayer.")
        to_lang = input(f"Entrez la langue cible {SUPPORTED_LANGUAGES['target']} (appuyez sur Entrée pour utiliser {default_to_lang}): ").strip().lower()
        if not to_lang:
            to_lang = default_to_lang

    return url, from_lang, to_lang

async def main():
    try:
        url, from_lang, to_lang = get_user_input()
        database_id = extract_database_id(url)
        await translate_all_pages_to_translate(database_id, from_lang, to_lang)
    except Exception as e:
        logger.error(f"Une erreur est survenue dans le programme principal : {str(e)}")
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())