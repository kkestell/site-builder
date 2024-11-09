import concurrent.futures
import hashlib
import json
import os
import shutil
import threading
import re
import xml.etree.ElementTree as ElementTree
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import List, Optional
import subprocess
from pathlib import Path
from xml.etree import ElementTree
import re
from markdown.treeprocessors import Treeprocessor

import markdown
from jinja2 import Environment, FileSystemLoader, Template
from recipy.latex import LatexOptions
from recipy.markdown import recipe_from_markdown
from recipy.models import Recipe
from recipy.pdf import recipe_to_pdf, PdfOptions
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from dataclasses import dataclass


@dataclass
class GalleryImage:
    image_path: str
    thumbnail_path: str


class Document:
    def __init__(self, frontmatter, content):
        self.frontmatter = frontmatter
        self.content = content
        self.content_hash = self._hash_content()

    def _hash_content(self):
        return hashlib.md5(self.content.encode()).hexdigest()


class FrontMatterParser:
    def __init__(self, filename):
        self.filename = Path(filename)

    def parse(self):
        with self.filename.open("r", encoding="utf-8") as file:
            lines = file.readlines()

        if lines[0].strip() == "---":
            end_frontmatter_idx = lines[1:].index("---\n") + 1
        else:
            raise ValueError("Frontmatter must start with '---'")

        frontmatter = {}
        for line in lines[1:end_frontmatter_idx]:
            key, value = line.strip().split(": ", 1)
            frontmatter[key] = value

        content = "".join(lines[end_frontmatter_idx + 2:])

        return Document(frontmatter, content)


class Node:
    def __init__(self, name: str, formatted_path: str, original_path: Path, order: int = 0):
        self.name = name
        self.formatted_path = formatted_path
        self.original_path = original_path
        self.parent = None
        self.order = order


class Directory(Node):
    def __init__(self, name: str, formatted_path: str, original_path: Path, children: List[Node] = None, order: int = 0):
        super().__init__(name, formatted_path, original_path, order)
        if children is None:
            children = []
        self.children = children

    def add_child(self, child: Node):
        child.parent = self
        self.children.append(child)


class File(Node):
    def __init__(self, formatted_path: str, original_path: Path, document: Document, updated_on: str):
        super().__init__(document.frontmatter.get("title", "Untitled"), formatted_path, original_path)
        self.document = document
        self.updated_on = updated_on


class ImageProcessor(Treeprocessor):
    def __init__(self, md, static_dir: Path):
        super().__init__(md)
        self.static_dir = static_dir

    def run(self, root):
        for image in root.iter('img'):
            src = image.get('src')
            alt = image.get('alt', '')

            # Skip external images
            if src.startswith(('http://', 'https://')):
                continue

            # Ensure `src` is a relative path under `static_dir`
            if src.startswith('/static/'):
                relative_path = src[len('/static/'):]
                image_path = self.static_dir / relative_path
            else:
                raise Exception(f"Unexpected image path format: {src}")

            # Verify the image file exists
            if not image_path.exists():
                raise Exception(f"Image file does not exist: {image_path}")

            # Generate dark variant path
            root_src, ext = os.path.splitext(src)
            dark_variant_src = f"{root_src}-dark{ext}"
            dark_image_path = self.static_dir / dark_variant_src[len('/static/'):]

            # Set `data-light` to the original `src` and check if a dark variant exists
            image.set('data-light', src)
            if dark_image_path.exists():
                image.set('class', 'responsive-image')
                image.set('style', 'opacity: 0')
                image.set('data-dark', dark_variant_src)
            
            # Set the `src` and `alt` attributes for final output
            image.set('src', src)
            image.set('alt', alt)


class ImageProcessorExtension(Extension):
    def __init__(self, static_dir: Path, output_dir: Path):
        self.static_dir = static_dir
        self.output_dir = output_dir
        super().__init__()

    def extendMarkdown(self, md):
        md.treeprocessors.register(
            ImageProcessor(md, self.static_dir),
            'image_processor',
            priority=15
        )


class SiteBuilder:
    def __init__(self, input_dir: Path, output_dir: Path, force: bool):
        self.pages_dir = input_dir / "pages"
        self.templates_dir = input_dir / "templates"
        self.static_dir = input_dir / "static"
        self.output_dir = output_dir
        self.root_directory = Directory("Home", "", "")
        self.jinja_env = Environment(loader=FileSystemLoader(str(self.templates_dir)))
        self.cache_file = output_dir / ".build_cache.json"
        self.force = force
        self.pdf_queue = Queue()
        self.image_queue = Queue()
        self.stop_event = threading.Event()
        self.sections = {}

    def build(self, async_mode: bool = False):
        """Build the site with optional asynchronous processing."""
        num_threads = os.cpu_count()

        if async_mode:
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                for _ in range(num_threads):
                    executor.submit(self._process_queues)
                self._execute_build_steps()
                self.pdf_queue.join()
                self.image_queue.join()
                self.stop_event.set()
        else:
            self._execute_build_steps()
            while not self.pdf_queue.empty() or not self.image_queue.empty():
                self._process_queues()
            self.stop_event.set()
            
        print("ok")

    def _execute_build_steps(self):
        self._build_structure(self.root_directory, self.pages_dir)
        self._build_html(self.root_directory)
        self._build_homepage()
        self._build_cooking_gallery()
        self._copy_static()

    def _has_featured(self, directory):
        for child in directory.children:
            if isinstance(child, File) and child.document.frontmatter.get('featured') == 'true':
                return True
            elif isinstance(child, Directory) and self._has_featured(child):
                return True
        return False

    def _build_cooking_gallery(self):
        gallery_source_dir = self.static_dir / "cooking/"
        if not gallery_source_dir.exists():
            print(f"Gallery source directory does not exist: {gallery_source_dir}")
            return

        # Target directories in output_dir
        target_dir = self.output_dir / "static/cooking/"
        thumbnails_dir = target_dir / "thumbnails"
        target_dir.mkdir(parents=True, exist_ok=True)
        thumbnails_dir.mkdir(parents=True, exist_ok=True)

        # Collect and preprocess images
        image_files = [f for f in gallery_source_dir.glob('*') if f.is_file()]
       
        # Preprocess images to rename with UNIX timestamp if needed
        for image_file in image_files:
            if image_file.suffix.lower() not in ['.jpg', '.jpeg']:
                raise ValueError(f"Unsupported image file type: {image_file}")

            filename = image_file.stem
            extension = image_file.suffix.lower()

            # Check if filename is already a timestamp (skip if so)
            if not filename.isdigit():
                # Retrieve create date, convert to UNIX timestamp, and rename
                timestamp = self._get_create_date(image_file)
                new_filename = f"{timestamp}{extension}"
                new_file_path = image_file.with_name(new_filename)

                # Rename the file if the new name doesn’t already exist
                if not new_file_path.exists():
                    image_file.rename(new_file_path)
                    print(f"Renamed {image_file} to {new_file_path}")

        # Refresh the list of images after preprocessing
        image_files = [f for f in gallery_source_dir.glob('*') if f.is_file()]
        image_files.sort(reverse=True)

        gallery_images = []

        for image_file in image_files:
            # Parse timestamp directly from filename now
            timestamp = int(image_file.stem)
            target_filename = f"{timestamp}.webp"

            source_path = image_file
            target_image_path = target_dir / target_filename
            thumbnail_path = thumbnails_dir / target_filename

            # Process image if the target or thumbnail does not exist
            if not target_image_path.exists() or not thumbnail_path.exists():
                self.image_queue.put((source_path, target_image_path, thumbnail_path))

            # Create GalleryImage
            gallery_image = GalleryImage(
                image_path=f"/static/cooking/{target_filename}",
                thumbnail_path=f"/static/cooking/thumbnails/{target_filename}"
            )
            gallery_images.append(gallery_image)

        # Wait for images to be processed
        self.image_queue.join()

        # Render the cooking.html template
        template = self.jinja_env.get_template("cooking.html")
        cooking_html = template.render(gallery_images=gallery_images)
        output_file = self.output_dir / "cooking.html"
        with output_file.open("w", encoding="utf-8") as f:
            f.write(cooking_html)
        print(output_file)


    def _process_image_item(self, source_path: Path, target_path: Path, thumbnail_path: Path):
        self._process_image(source_path, target_path, thumbnail_path)
        print(target_path)

    def _process_image(self, source_path: Path, target_path: Path, thumbnail_path: Path):
        try:
            full_size_cmd = [
                'convert', str(source_path),
                '-auto-orient',
                '-resize', '1920x1080>',
                '-strip',
                '-define', 'webp:method=6',
                '-define', 'webp:lossless=false',
                '-define', 'webp:alpha-quality=85',
                '-define', 'webp:preprocessing=4',
                '-quality', '75',
                f'{str(target_path)}'
            ]
            subprocess.run(full_size_cmd, check=True, capture_output=True)

            thumbnail_cmd = [
                'convert', str(source_path),
                '-auto-orient',
                '-resize', '350x350^',
                '-gravity', 'center',
                '-extent', '350x350',
                '-strip',
                '-define', 'webp:method=6',
                '-define', 'webp:lossless=false',
                '-define', 'webp:alpha-quality=80',
                '-define', 'webp:preprocessing=4',
                '-quality', '60',
                f'{str(thumbnail_path)}'
            ]
            subprocess.run(thumbnail_cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Error processing image {source_path}:")
            print(f"Command failed with return code {e.returncode}")
            print(f"Error output: {e.stderr.decode()}")
            raise
        except Exception as e:
            print(f"Unexpected error processing image {source_path}: {str(e)}")
            raise

    def _get_create_date(self, image_path: Path) -> int:
        result = subprocess.run(['exiftool', str(image_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output = result.stdout

        match = re.search(r'Create Date\s+:\s+(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})', output)
        if match:
            create_date_str = match.group(1)
            create_date = datetime.strptime(create_date_str, '%Y:%m:%d %H:%M:%S')
            timestamp = int(create_date.timestamp())
            return timestamp
        else:
            raise ValueError(f"No 'Create Date' found in EXIF data for {image_path}")

    def _build_structure(self, current_directory: Directory, current_path: Path):
        for item in current_path.iterdir():
            name = item.stem
            formatted_name = name.replace("-", " ").title()
            original_path = item.relative_to(self.pages_dir)
            formatted_path = original_path.as_posix()

            if item.is_dir():
                directory = Directory(formatted_name, formatted_path, original_path)
                current_directory.add_child(directory)
                self._build_structure(directory, item)
            elif item.is_file() and item.suffix == ".md":
                document = FrontMatterParser(item).parse()
                if document.frontmatter.get("draft", "false").lower() == "true":
                    continue
                updated_on = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                file = File(formatted_path, original_path, document, updated_on)
                current_directory.add_child(file)
        self._sort_structure(current_directory)

    def _sort_structure(self, directory: Directory):
        directory.children.sort(key=lambda n: (n.order, f"0{n.name}" if isinstance(n, Directory) else f"1{n.name}"))
        for child in directory.children:
            if isinstance(child, Directory):
                self._sort_structure(child)

    def _build_html(self, current_directory: Directory):
        for child in current_directory.children:
            if isinstance(child, Directory):
                directory_path = self.output_dir / child.formatted_path
                directory_path.mkdir(parents=True, exist_ok=True)
                self._build_html(child)
                self._build_index(child, directory_path)
            elif isinstance(child, File):
                output_file = (self.output_dir / child.formatted_path).with_suffix(".html")

                if not self.force and output_file.exists():
                    output_file_mtime = output_file.stat().st_mtime
                    source_file_mtime = (self.pages_dir / child.original_path).stat().st_mtime
                    if output_file_mtime >= source_file_mtime:
                        continue

                self._build_page(child, output_file)

    def _build_index(self, directory: Directory, output_path: Path):
        content = self._generate_list(directory)
        breadcrumbs = self._generate_breadcrumbs(directory)
        template = self.jinja_env.get_template("index.html")
        meta = {}
        if directory.parent:
            meta_file = self.pages_dir / directory.original_path / "meta.json"
            if meta_file.exists():
                with open(meta_file, 'r') as f:
                    meta = json.load(f)
        index_html = template.render(
            content=content,
            title=directory.name,
            breadcrumbs=breadcrumbs,
            description=meta.get("description", ""),
        )
        with open(output_path / "index.html", "w", encoding="utf-8") as f:
            f.write(index_html)

    def _build_normal_page(self, template: Template, breadcrumbs: str, file: File):
        content = markdown.markdown(
            file.document.content,
            extensions=[
                "extra",
                ImageProcessorExtension(static_dir=self.static_dir, output_dir=self.output_dir),
            ]
        )
        html_content = template.render(
            content=content,
            breadcrumbs=breadcrumbs,
            updated_on=file.updated_on,
            **file.document.frontmatter,
        )
        return html_content

    def _generate_pdf(self, recipe: Recipe, pdf_path: Path, source_date_epoch: Optional[str] = "0"):
        self.pdf_queue.put((recipe, pdf_path, source_date_epoch))

    def _process_pdf_item(self, recipe, pdf_path, source_date_epoch):
        pdf_path = self.output_dir / pdf_path
        latex_options = LatexOptions(main_font="Source Serif Pro", heading_font="Source Sans Pro")
        pdf_options = PdfOptions(reproducible=True)
        pdf_data = recipe_to_pdf(recipe, latex_options=latex_options, pdf_options=pdf_options)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with pdf_path.open("wb") as f:
            f.write(pdf_data)
        print(pdf_path)

    def _process_queues(self):
        while not self.stop_event.is_set():
            processed_something = False
            try:
                recipe, pdf_path, source_date_epoch = self.pdf_queue.get(timeout=1)
                self._process_pdf_item(recipe, pdf_path, source_date_epoch)
                self.pdf_queue.task_done()
                processed_something = True
            except Empty:
                pass
            try:
                image_args = self.image_queue.get(timeout=1)
                self._process_image_item(*image_args)
                self.image_queue.task_done()
                processed_something = True
            except Empty:
                pass
            if not processed_something and self.pdf_queue.empty() and self.image_queue.empty():
                break

    def _build_recipe_page(self, template: Template, breadcrumbs: str, file: File):
        recipe = recipe_from_markdown(file.document.content)
        if not recipe:
            raise ValueError(f"Failed to parse recipe: {file.original_path}")
        pdf_path = Path(f"static/{file.formatted_path.replace('.md', '.pdf')}")
        source_date_epoch = str(int(datetime.strptime(file.updated_on, "%Y-%m-%d %H:%M:%S").timestamp()))
        self._generate_pdf(recipe, pdf_path, source_date_epoch)
        html_content = template.render(
            recipe=recipe,
            pdf_path=pdf_path,
            breadcrumbs=breadcrumbs,
            updated_on=file.updated_on,
            **file.document.frontmatter,
        )
        return html_content

    def _build_page(self, file: File, output_file: Path):
        template_name = file.document.frontmatter.get("template", "page")
        template = self.jinja_env.get_template(f"{template_name}.html")
        breadcrumbs = self._generate_breadcrumbs(file)

        if template_name == "recipe":
            html_content = self._build_recipe_page(template, breadcrumbs, file)
        else:
            html_content = self._build_normal_page(template, breadcrumbs, file)

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("w", encoding="utf-8") as f:
            f.write(html_content)

        print(output_file)

    def _compute_has_mixed_featured(self, directory: Directory):
        files = self._get_all_files(directory)
        has_featured = any(f.document.frontmatter.get("featured", "false") == "true" for f in files)
        has_non_featured = any(f.document.frontmatter.get("featured", "false") != "true" for f in files)
        directory.has_mixed_featured = has_featured and has_non_featured

    def _get_all_files(self, directory: Directory):
        files = []
        for child in directory.children:
            if isinstance(child, File):
                files.append(child)
            elif isinstance(child, Directory):
                files.extend(self._get_all_files(child))
        return files

    def _build_homepage(self):
        for section in self.root_directory.children:
            if isinstance(section, Directory):
                self._compute_has_mixed_featured(section)

        self.sections = {
            child.name.lower(): child for child in self.root_directory.children if isinstance(child, Directory) and self._has_featured(child)
        }

        home_template = self.jinja_env.get_template("home.html")
        home_html = home_template.render(sections=self.sections)
        with open(self.output_dir / "index.html", "w", encoding="utf-8") as f:
            f.write(home_html)

    def _copy_static(self):
        target_dir = self.output_dir / "static"
        target_dir.mkdir(parents=True, exist_ok=True)

        for item in os.listdir(self.static_dir):
            if item == "cooking":
                continue

            s = Path(self.static_dir) / item
            d = target_dir / item
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

    def _generate_list(self, directory: Directory):
        html_builder = ["<ul>"]
        for child in directory.children:
            if isinstance(child, Directory):
                dir_link = f"/{child.formatted_path}/index.html"
                html_builder.append(f'<li class="dir"><h2><a href="{dir_link}">{child.name}</a></h2>{self._generate_list(child)}</li>')
            elif isinstance(child, File):
                page_path = child.formatted_path.replace(".md", ".html")
                title = child.name
                title_link = f'<a href="/{page_path}">{title}</a>'
                if child.document.frontmatter.get("featured"):
                    title_link = f"<i>⭐</i>{title_link}"
                else:
                    title_link = f"<i></i>{title_link}"
                html_builder.append(f'<li>{title_link}')
                if child.document.frontmatter.get("subtitle"):
                    html_builder.append(f"<span>{child.document.frontmatter['subtitle']}</span>")
                html_builder.append("</li>")
        html_builder.append("</ul>")
        return "".join(html_builder)

    def _generate_breadcrumbs(self, node: Node):
        breadcrumbs = []
        current_node = node.parent
        while current_node:
            breadcrumbs.append(f'<a href="/{current_node.formatted_path}">{current_node.name}</a>')
            current_node = current_node.parent
        breadcrumbs = breadcrumbs[::-1]
        breadcrumbs.append(node.name)
        return ' <span class="separator">/</span> '.join(breadcrumbs)


def main():
    parser = ArgumentParser(description="Static site generator")
    parser.add_argument("-i", "--input", default="./content/", help="Input directory path")
    parser.add_argument("-o", "--output", default="./dist/", help="Output directory path")
    parser.add_argument("-f", "--force", action="store_true", help="Force all pages and PDFs to be rebuilt")
    args = parser.parse_args()

    builder = SiteBuilder(Path(args.input), Path(args.output), args.force)
    builder.build(async_mode=True)


if __name__ == "__main__":
    main()
