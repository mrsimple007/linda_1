"""Voice chess: parse spoken moves, play with Stockfish (if installed) or a weak fallback."""
import random, re, shutil
import chess, chess.engine

CYR = str.maketrans({"а": "a", "с": "c", "е": "e"})      # Cyrillic look-alike letters
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
        b, t = self.board, text.lower().translate(CYR)
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
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9=+#-]*", text):   # SAN like Nf3, exd5, e4
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
