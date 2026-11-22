"""Voice chess: parse spoken moves, play with Stockfish (if installed) or a weak fallback."""
import random, re, shutil
import chess, chess.engine

CYR = str.maketrans({"а": "a", "с": "c", "е": "e"})      # Cyrillic look-alike letters
SPOKEN = {                                                 # Russian speech recognition: "е два е четыре", "конь эф три"
    "a": ("а",), "b": ("бэ", "бе", "би"), "c": ("цэ", "це", "си", "сэ"), "d": ("дэ", "де", "ди"),
    "e": ("е", "э", "е"), "f": ("эф", "эфф", "ef"), "g": ("жэ", "же", "гэ", "ге", "джи"), "h": ("аш", "ха", "эйч"),
    "1": ("один", "одна", "раз"), "2": ("два", "две"), "3": ("три",), "4": ("четыре",), "5": ("пять",),
    "6": ("шесть",), "7": ("семь",), "8": ("восемь",),
}
_WORD = {w: k for k, ws in SPOKEN.items() for w in ws}


def spoken(text):
    """'е два е четыре' -> 'e2 e4'; 'конь эф 3' -> 'конь f3'. Letters and numbers said as words become squares."""
    toks = [_WORD.get(w, w) for w in re.findall(r"[\w']+", text.lower())]
    out = " ".join(toks)
    return re.sub(r"\b([a-h])\s+([1-8])\b", r"\1\2", out)
PIECE_WORDS = {
    chess.KNIGHT: r"knight|конь|коня|\bot\b",
    chess.BISHOP: r"bishop|слон|\bfil\b",
    chess.ROOK: r"rook|ладь|\brux\b",
    chess.QUEEN: r"queen|ферз|farzin",
    chess.KING: r"king|король|короля|shoh",
    chess.PAWN: r"pawn|пешк|piyoda",
}
NAMES = {
    "en": {1: "pawn", 2: "knight", 3: "bishop", 4: "rook", 5: "queen", 6: "king"},
    "ru": {1: "пешка", 2: "конь", 3: "слон", 4: "ладья", 5: "ферзь", 6: "король"},
    "uz": {1: "piyoda", 2: "ot", 3: "fil", 4: "rux", 5: "farzin", 6: "shoh"},
}
TAKES = {"en": "takes", "ru": "бьёт", "uz": "oladi"}
CASTLE = {"en": "castles", "ru": "рокировка", "uz": "rokirovka"}


class ChessGame:
    def __init__(self, user_white=True, skill=5, think=0.3):
        self.board, self.user_white, self.think, self.engine = chess.Board(), user_white, think, None
        path = shutil.which("stockfish")
        if path:
            self.engine = chess.engine.SimpleEngine.popen_uci(path)
            self.engine.configure({"Skill Level": skill})
        else:
            print("[chess] Stockfish not found (brew install stockfish): playing a weak fallback.")

    def close(self):
        if self.engine:
            try:
                self.engine.quit()
            except Exception:
                pass

    def show(self):
        print("\n" + str(self.board) + "\n")

    def legal_san(self):
        return [self.board.san(m) for m in self.board.legal_moves]

    def parse(self, text):
        """Spoken text -> legal Move or None. Handles 'e2 to e4', 'Nf3', 'exd5', 'knight f3', castling."""
        b, t = self.board, spoken(text).translate(CYR)
        if re.search(r"castl|рокиров|rokirov", t):
            long = re.search(r"long|queen|длинн|uzun", t)
            return self._san("O-O-O" if long else "O-O")
        sq = re.findall(r"([a-h])\s*([1-8])", t)
        if len(sq) == 2:                                   # explicit from/to squares
            uci = "".join(f + r for f, r in sq)
            for cand in (uci, uci + "q"):
                try:
                    mv = chess.Move.from_uci(cand)
                except ValueError:
                    return None
                if mv in b.legal_moves:
                    return mv
            return None
        piece = next((pt for pt, rx in PIECE_WORDS.items() if re.search(rx, t)), None)
        if len(sq) == 1 and piece:                         # "knight f3", "конь f3", "pawn takes e5"
            dest = chess.parse_square(sq[0][0] + sq[0][1])
            moves = [m for m in b.legal_moves
                     if m.to_square == dest and b.piece_type_at(m.from_square) == piece]
            return moves[0] if len(moves) == 1 else None
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9=+#-]*", f"{text} {t}"):   # SAN like Nf3, exd5, e4 (also 'е4')
            for cand in (tok, tok[0].upper() + tok[1:], tok[0].lower() + tok[1:]):
                mv = self._san(cand)
                if mv:
                    return mv
        return None

    def _san(self, s):
        try:
            return self.board.parse_san(s)
        except ValueError:
            return None

    def move_from_san(self, san):
        return self._san(san)

    def engine_move(self):
        if self.engine:
            return self.engine.play(self.board, chess.engine.Limit(time=self.think)).move
        moves = list(self.board.legal_moves)
        caps = [m for m in moves if self.board.is_capture(m)]
        return random.choice(caps or moves)


def speak_move(board, move, lang):
    """Describe a move BEFORE it is pushed, e.g. 'knight f 3' / 'конь бьёт e 5'."""
    if board.is_castling(move):
        return CASTLE[lang]
    piece = NAMES[lang][board.piece_type_at(move.from_square)]
    sq = chess.square_name(move.to_square)
    take = f" {TAKES[lang]}" if board.is_capture(move) else ""
    return f"{piece}{take} {sq[0]} {sq[1]}"


# ---------- the chess session inside Agent (agent.chess, agent.bw) ----------
CHESS_STOP = ("stop", "resign", "quit", "стоп", "хватит", "сдаюсь", "to'xta", "toxta", "tugat")
CHESS_UNDO = ("undo", "take back", "назад", "верни ход", "qaytar", "orqaga")
CHESS_HINT = ("hint", "best move", "подсказ", "maslahat")


class BoardWindow:
    def __init__(self):
        # Initialize your window properties here
        pass


class ChessMixin:
    """Voice chess turns: start, stop, undo, hint, your move, Linda's move. The board shows in agent.bw."""

    def _show(self):
        g = self.chess
        g.show()
        if self.bw:
            last = g.board.peek().uci() if g.board.move_stack else None
            status = "Your move" if g.board.turn == g.user_white else "Linda is thinking…"
            if g.board.is_check():
                status += " – check!"
            if g.board.is_game_over():
                status = f"Game over: {g.board.result()}"
            self.bw.update(g.board.fen(), last, status, flip=not g.user_white)

    def do_chess(self, it, text):
        if self.chess:
            self.chess.close()
        c = self.cfg["chess"]
        white = it.color != "black"
        self.chess = ChessGame(white, c["skill_level"], c["think_time"])
        if c.get("window", True):
            self.bw = self.bw or BoardWindow()
            self.bw.open()
        self._show()
        self.k("chess_white" if white else "chess_black")
        if not white:
            self._engine_turn()

    def _log_chess(self, result):
        g = self.chess
        if not g or len(g.board.move_stack) < 4:
            return
        import chess.pgn
        pgn = str(chess.pgn.Game.from_board(g.board))
        self.store.log_chess(g.user_white, self.cfg["chess"]["skill_level"], result, len(g.board.move_stack), pgn)

    def chess_turn(self, text, cloud=True):
        low, g = text.lower(), self.chess
        if any(w in low for w in CHESS_STOP):
            self._log_chess("abandoned")
            g.close()
            self.chess = None
            if self.bw:
                self.bw.close()
            self.k("chess_stop")
            return True
        if any(w in low for w in CHESS_UNDO):
            if len(g.board.move_stack) >= 2:
                g.board.pop(); g.board.pop()
                self._show()
                self.k("chess_undo")
            else:
                self.k("chess_illegal")
            return True
        if any(w in low for w in CHESS_HINT):
            self.k("chess_hint", move=speak_move(g.board, g.engine_move(), self.lang))
            return True
        mv = g.parse(text) or (self._gemini_move(text) if cloud else None)    # Alice can't wait for Gemini
        if mv is None:
            self.k("chess_illegal")
            return True
        g.board.push(mv)
        self._show()
        if not self._finished():
            self._engine_turn()
        return True                                     # the move was the whole turn: nothing else may answer it
        return True

    def _gemini_move(self, text):
        """Fallback for phrases like 'конь эф три': Gemini picks one of the legal moves."""
        if not self.cloud():
            return None
        g = self.chess
        legal = g.legal_san()
        prompt = (f"Chess position (FEN): {g.board.fen()}\nLegal moves (SAN): {', '.join(legal)}\n"
                  f"The player said (English/Russian/Uzbek, may be misheard): \"{text}\"\n"
                  "Reply with exactly one legal move from the list, or NONE.")
        try:
            ans = self.gemini.text(prompt, max_out=20).strip().strip(".")
        except Exception:
            return None
        return g.move_from_san(ans) if ans in legal else None

    def _engine_turn(self):
        g = self.chess
        mv = g.engine_move()
        desc = speak_move(g.board, mv, self.lang)
        g.board.push(mv)
        self._show()
        self.k("chess_my_move", move=desc)
        if not self._finished() and g.board.is_check():
            self.k("chess_check")

    def _finished(self):
        g = self.chess
        if not g.board.is_game_over():
            return False
        w = g.board.outcome().winner
        key = "chess_draw" if w is None else ("chess_win_user" if w == g.user_white else "chess_win_me")
        self.k(key)
        self._log_chess({"chess_draw": "draw", "chess_win_user": "win"}.get(key, "loss"))
        g.close()
        self.chess = None                                  # board window stays open until you close it
        return True
