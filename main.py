import os
import asyncio
import logging
from datetime import datetime
from notion_client import AsyncClient
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from deepl import Translator
from flask import Flask, request, jsonify

app = Flask(__name__)

# Configuration du logging
log_filename = f"translation_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler(log_filename),
                        logging.StreamHandler(sys.stdout)
                    ])
logger = logging.getLogger(__name__)

# Récupération des variables d'environnement
NOTION_API_TOKEN = os.environ.get('NOTION_API_TOKEN')
DEEPL_API_KEY = os.environ.get('DEEPL_API_KEY')

if not all([NOTION_API_TOKEN, DEEPL_API_KEY]):
    logger.error(
        "Variables d'environnement manquantes. Assurez-vous de définir NOTION_API_TOKEN et DEEPL_API_KEY."
    )
    sys.exit(1)

# Initialisation des clients API
notion = AsyncClient(auth=NOTION_API_TOKEN)
translator = Translator(DEEPL_API_KEY)

# Création d'un ThreadPoolExecutor global
thread_pool = ThreadPoolExecutor(max_workers=5)

# Constantes
SUPPORTED_LANGUAGES = {
    'source': [
        'bg', 'cs', 'da', 'de', 'el', 'en', 'es', 'et', 'fi', 'fr', 'hu', 'id',
        'it', 'ja', 'lt', 'lv', 'nl', 'pl', 'pt', 'ro', 'ru', 'sk', 'sl', 'sv',
        'tr', 'zh'
    ],
    'target': [
        'bg', 'cs', 'da', 'de', 'el', 'en-gb', 'en-us', 'es', 'et', 'fi', 'fr',
        'hu', 'id', 'it', 'ja', 'lt', 'lv', 'nl', 'pl', 'pt-pt', 'pt-br', 'ro',
        'ru', 'sk', 'sl', 'sv', 'tr', 'zh'
    ]
}

LANGUAGE_NAMES = {
    'fr': 'Français',
    'nl': 'Nederlands',
    # Ajoutez d'autres langues si nécessaire
}


def extract_database_id(url):
    """Extrait l'ID de la base de données à partir de l'URL Notion."""
    match = re.search(r'([a-f0-9]{32})', url)
    if match:
        return match.group(1)
    raise ValueError(
        "URL Notion invalide. Assurez-vous qu'elle contient un ID de base de données valide."
    )


async def get_all_pages(database_id):
    """Obtient toutes les pages d'une base de données."""
    try:
        pages = []
        has_more = True
        start_cursor = None

        while has_more:
            response = await notion.databases.query(database_id=database_id,
                                                    start_cursor=start_cursor)
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
            page
            for page in all_pages if page['properties'].get('Statut', {}).get(
                'status', {}).get('name') == 'A traduire'
        ]
        logger.info(
            f"Nombre de pages à traduire trouvées : {len(pages_to_translate)}")
        return pages_to_translate
    except Exception as e:
        logger.error(
            f"Erreur lors de la récupération des pages à traduire : {str(e)}")
        raise


async def translate_text(text, from_lang, to_lang):
    """Traduit un texte en utilisant l'API DeepL de manière asynchrone."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            thread_pool, lambda: translator.translate_text(
                text, source_lang=from_lang, target_lang=to_lang))
        return result.text
    except Exception as e:
        logger.error(f"Erreur lors de la traduction du texte : {str(e)}")
        raise


async def translate_block(block, from_lang, to_lang):
    """Traduit un bloc Notion."""
    try:
        if block['type'] in [
                'paragraph', 'heading_1', 'heading_2', 'heading_3',
                'bulleted_list_item', 'numbered_list_item', 'to_do', 'toggle',
                'quote', 'callout'
        ]:
            text_elements = block[block['type']]['rich_text']
            for text_element in text_elements:
                if text_element['type'] == 'text':
                    original_text = text_element['text']['content']
                    translated_text = await translate_text(
                        original_text, from_lang, to_lang)
                    text_element['text']['content'] = translated_text
            return block
        else:
            return None
    except Exception as e:
        logger.error(
            f"Erreur lors de la traduction du bloc {block['id']}: {str(e)}")
        raise


async def translate_page(page_id, from_lang, to_lang):
    """Traduit directement une page Notion."""
    try:
        page = await notion.pages.retrieve(page_id=page_id)
        updated_properties = {}
        for prop_name, prop_value in page['properties'].items():
            if prop_value['type'] == 'title':
                if prop_value['title']:
                    original_text = prop_value['title'][0]['plain_text']
                    translated_text = await translate_text(
                        original_text, from_lang, to_lang)
                    updated_properties[prop_name] = {
                        'title': [{
                            'text': {
                                'content': translated_text
                            }
                        }]
                    }
            elif prop_value['type'] == 'rich_text':
                if prop_value['rich_text']:
                    original_text = ''.join([
                        text['plain_text'] for text in prop_value['rich_text']
                    ])
                    translated_text = await translate_text(
                        original_text, from_lang, to_lang)
                    updated_properties[prop_name] = {
                        'rich_text': [{
                            'text': {
                                'content': translated_text
                            }
                        }]
                    }

        if updated_properties:
            await notion.pages.update(page_id=page_id,
                                      properties=updated_properties)
            logger.info(
                f"Propriétés de la page traduites avec succès: {page_id}")
        else:
            logger.warning(
                f"Aucune propriété modifiable trouvée pour la page {page_id}.")

        blocks = await notion.blocks.children.list(block_id=page_id)
        for block in blocks['results']:
            translated_block = await translate_block(block, from_lang, to_lang)
            if translated_block:
                await notion.blocks.update(block_id=block['id'],
                                           **translated_block)

        await notion.pages.update(page_id=page_id,
                                  properties={
                                      "Langue": {
                                          "select": {
                                              "name":
                                              LANGUAGE_NAMES.get(
                                                  to_lang, to_lang)
                                          }
                                      },
                                      "Statut": {
                                          "status": {
                                              "name": "Traduit"
                                          }
                                      }
                                  })

        return {"page_id": page_id, "status": "success"}
    except Exception as e:
        logger.error(
            f"Erreur lors de la traduction de la page {page_id}: {str(e)}")
        return {"page_id": page_id, "status": "error", "error_message": str(e)}


async def translate_all_pages_to_translate(database_id, from_lang, to_lang):
    """Traduit toutes les pages avec le statut 'A traduire' d'une base de données."""
    try:
        pages = await get_pages_to_translate(database_id)
        results = []
        for page in pages:
            result = await translate_page(page['id'], from_lang, to_lang)
            results.append(result)
        return results
    except Exception as e:
        logger.error(f"Erreur lors de la traduction des pages : {str(e)}")
        raise


async def translate_specific_pages(page_ids, from_lang, to_lang):
    """Traduit des pages spécifiques."""
    try:
        results = []
        for page_id in page_ids:
            result = await translate_page(page_id, from_lang, to_lang)
            results.append(result)
        return results
    except Exception as e:
        logger.error(
            f"Erreur lors de la traduction des pages spécifiques : {str(e)}")
        raise


@app.route('/translate', methods=['POST'])
def translate():
    data = request.json
    database_url = data.get('database_url')
    from_lang = data.get('from_lang', 'fr')
    to_lang = data.get('to_lang', 'nl')
    mode = data.get('mode', 'auto')
    page_ids = data.get('page_ids', [])

    if mode not in ['auto', 'manual']:
        return jsonify({"error": "Invalid mode. Use 'auto' or 'manual'."}), 400

    if mode == 'manual' and not page_ids:
        return jsonify({"error": "page_ids is required for manual mode"}), 400

    if mode == 'auto' and not database_url:
        return jsonify({"error":
                        "database_url is required for auto mode"}), 400

    try:
        if mode == 'auto':
            database_id = extract_database_id(database_url)
            results = asyncio.run(
                translate_all_pages_to_translate(database_id, from_lang,
                                                 to_lang))
        else:
            results = asyncio.run(
                translate_specific_pages(page_ids, from_lang, to_lang))

        return jsonify({"status": "success", "results": results})
    except Exception as e:
        logger.error(f"Erreur dans le processus de traduction : {str(e)}")
        return jsonify({"status": "error", "error_message": str(e)}), 500


def run_translation_script():
    """Fonction pour exécuter le script de traduction manuellement."""
    print("Bienvenue dans le script de traduction Notion!")
    print("Veuillez choisir un mode de fonctionnement:")
    print("1. Mode automatique (traduire toutes les pages 'A traduire')")
    print("2. Mode manuel (traduire des pages spécifiques)")

    choice = input("Entrez votre choix (1 ou 2): ")

    if choice == '1':
        database_url = input("Entrez l'URL de la base de données Notion: ")
        from_lang = input(
            "Entrez la langue source (par défaut 'fr'): ") or 'fr'
        to_lang = input("Entrez la langue cible (par défaut 'nl'): ") or 'nl'

        try:
            database_id = extract_database_id(database_url)
            results = asyncio.run(
                translate_all_pages_to_translate(database_id, from_lang,
                                                 to_lang))
            print("Résultats de la traduction:")
            print(results)
        except Exception as e:
            print(f"Une erreur s'est produite: {str(e)}")

    elif choice == '2':
        page_ids = input(
            "Entrez les IDs des pages à traduire (séparés par des virgules): "
        ).split(',')
        from_lang = input(
            "Entrez la langue source (par défaut 'fr'): ") or 'fr'
        to_lang = input("Entrez la langue cible (par défaut 'nl'): ") or 'nl'

        try:
            results = asyncio.run(
                translate_specific_pages(page_ids, from_lang, to_lang))
            print("Résultats de la traduction:")
            print(results)
        except Exception as e:
            print(f"Une erreur s'est produite: {str(e)}")

    else:
        print("Choix invalide. Veuillez relancer le script et choisir 1 ou 2.")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'run_script':
        run_translation_script()
    else:
        app.run(debug=True)
