"""Render the actual local nine-frame skin switch without opening the live profile."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication
from shu.pet import FloatingPet
from shu.skins import SkinCatalog, TRANSITION_FRAME_MS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-skin',default='default-corrected')
    parser.add_argument('--to-skin',default='purple')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--pillow-path',type=Path,help='Optional existing Pillow dependency directory for GIF encoding')
    args=parser.parse_args()
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    original=ROOT/'assets/spritesheet.png'
    catalog=SkinCatalog(original)
    source,target=catalog.get(args.from_skin),catalog.get(args.to_skin)
    if source is None or target is None:parser.error('Requested skin is missing from the local manifest')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    pet=FloatingPet(original);pet.canvas.timer.stop();pet._hover_timer.stop()
    try:
        with TemporaryDirectory() as temporary:
            pet.configure_skins(Path(temporary)/'pet-appearance.json',catalog=catalog)
            if not pet.canvas.set_skin(source,animate=False) and pet.canvas.skin_id!=source.id:
                raise RuntimeError('Could not load source skin')
            with patch('shu.pet.time.monotonic',return_value=100.):
                if not pet.select_skin(target.id) or not pet.canvas.skin_switching:
                    raise RuntimeError('Both generated transition strips are required for a nine-frame preview')
            sheet=QImage(192*9,232,QImage.Format.Format_ARGB32)
            sheet.fill(QColor('#f7f4ef'));painter=QPainter(sheet)
            files=[]
            for index in range(9):
                with patch('shu.pet.time.monotonic',return_value=100.+index*.1+.001):pet.canvas._tick()
                frame=pet.canvas.displayed_frame()
                output=args.output_dir/f'{index:02}.png'
                if not frame.save(str(output)):raise OSError('Could not save preview frame')
                painter.drawPixmap(index*192,24,frame)
                painter.setPen(QColor('#26342f'));painter.drawText(index*192+8,16,f'{index+1} / 9')
                files.append(str(output))
            painter.end();sheet.save(str(args.output_dir/'skin-switch-contact-sheet.png'))
            if args.pillow_path:sys.path.insert(0,str(args.pillow_path.resolve()))
            from PIL import Image
            gif_frames=[]
            for filename in files:
                rgba=Image.open(filename).convert('RGBA')
                preview=Image.new('RGB',rgba.size,'#f7f4ef');preview.paste(rgba,(0,0),rgba)
                gif_frames.append(preview)
            gif= args.output_dir/'skin-switch.gif'
            gif_frames[0].save(gif,save_all=True,append_images=gif_frames[1:],duration=TRANSITION_FRAME_MS,loop=0,optimize=False)
            with patch('shu.pet.time.monotonic',return_value=101.):pet.canvas._tick()
            result={'ok':pet.canvas.skin_id==target.id,'offline':True,'profile':'isolated temporary directory',
                    'source':source.id,'target':target.id,'frames':files,'frame_ms':TRANSITION_FRAME_MS,
                    'gif':str(gif),'committed_skin':pet.canvas.skin_id,'network_requests':0,'game_inputs':0}
            (args.output_dir/'preview.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=True))
            return 0 if result['ok'] else 1
    finally:
        pet.close();pet.deleteLater();app.processEvents()


if __name__=='__main__':raise SystemExit(main())
