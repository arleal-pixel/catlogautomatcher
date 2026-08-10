#!/usr/bin/env python3
"""
Discriminador de descripcion por (MODELO, ANIO).

Problema: la tablota me da MODELO + ANIO. Con eso filtro un conjunto de
descripciones candidatas. Si hay >1, quiero identificar CUAL aplica haciendo
el MINIMO numero de preguntas al corredor.

Enfoque: arbol de decision greedy por ganancia de informacion, pero en lugar
de preguntar token-por-token (binario), agrupo los tokens en FAMILIAS de
atributos (trim, transmision, motor, carroceria, puertas). En cada paso
pregunto la familia que mejor parte el conjunto restante -> preguntas
categoricas naturales para el corredor y muy pocas.

Es una prueba de concepto sobre la tablota real; los catalogos de familias
(TRANS, CARR, etc.) se pueden enriquecer con trims_catalog.py / vocabulario
del proyecto.
"""
import csv, re, math
from collections import defaultdict, Counter

# --- Familias de atributos (vocabulario base, extensible) ---
TRANS = {"AUT","STD","CVT","DSG","TIPTRONIC","MANUAL","SECUENCIAL","AT","MT",
         "DCT","MULTITRONIC","STRONIC","S-TRONIC","XTRONIC","AUTOMATICO"}
CARR  = {"SUV","SEDAN","HB","HATCHBACK","COUPE","CONV","CONVERTIBLE","VAN",
         "SPORTBACK","SW","WAGON","PICKUP","ROADSTER","CABRIO","FASTBACK","LIFTBACK"}
# motor: cilindros, cilindrada, alimentacion
MOTOR_RE = re.compile(r'^(V\d{1,2}|L\d|I\d|W\d{1,2}|R\d)$')
CILINDRADA_RE = re.compile(r'^\d\.\d[TL]?$')
ALIM = {"IMP","TUR","TURBO","BITURBO","TDI","TSI","TFSI","FSI","GDI","HYBRID",
        "HIBRIDO","MHEV","PHEV","ELECTRICO","EV","TDCI","CRDI","DCI","VVT"}
# equipamiento de bajo valor discriminante (casi siempre compartido)
EQUIPO = {"ABS","CA","CE","PIEL","TELA","SM","SQ","CB","CD","CQ","CT","SB",
          "D/V","BA","AC","EE","QC","GPS","RA","F.NIEBLA","FN","IPC","4X4",
          "4X2","AA","DH","VP","RIN"}

def familia(tok):
    t=tok.upper()
    if t in TRANS: return "TRANSMISION"
    if t in CARR:  return "CARROCERIA"
    if MOTOR_RE.match(t) or CILINDRADA_RE.match(t): return "MOTOR"
    if t in ALIM:  return "ALIMENTACION"
    if t in EQUIPO: return None            # ignorado
    if re.fullmatch(r'[2-5]', t): return "PUERTAS"   # digito suelto = puertas
    if re.fullmatch(r'\d{2,}', t): return None       # numeros largos = ruido
    return "TRIM"                          # lo demas: nombre de version

def atributos(desc, linea):
    """Devuelve dict familia -> valor (string) para una descripcion.
    Quita el nombre de la linea del inicio para no tokenizarlo como trim."""
    d = desc.upper()
    toks = [t for t in re.split(r'[\s]+', d) if t]
    # pelar tokens de la linea si aparecen al inicio
    linetoks = linea.upper().split()
    i=0
    while i < len(toks) and i < len(linetoks) and toks[i]==linetoks[i]:
        i+=1
    toks = toks[i:]
    # pelar prefijos de generacion tipo "MK VI", "MK", numeros romanos sueltos
    fam = defaultdict(list)
    for t in toks:
        f = familia(t)
        if f: fam[f].append(t)
    return {k: " ".join(v) for k,v in fam.items()}

def entropia(cnt):
    n=sum(cnt);
    return -sum((c/n)*math.log2(c/n) for c in cnt if c)

def mejor_pregunta(cands):
    """cands: lista de (clave, desc, attrs). Elige la familia con mayor
    ganancia de informacion (particion mas uniforme del conjunto)."""
    n=len(cands)
    familias = set()
    for _,_,a in cands: familias.update(a.keys())
    mejor=None
    # sorted(): iterar un set() de strings sin ordenar depende del hash
    # randomization de Python (PYTHONHASHSEED), que cambia por proceso. En
    # empates genuinos (mismo score) eso hace que la familia elegida -y por
    # lo tanto la pregunta hecha al corredor- varie entre corridas para la
    # MISMA entrada. sorted() fija el desempate (alfabetico) de forma
    # reproducible sin cambiar el resultado cuando no hay empate.
    for fam in sorted(familias):
        grupos=Counter(a.get(fam,"—") for _,_,a in cands)
        if len(grupos)<2:      # no discrimina
            continue
        # ganancia = H(total) - H(condicional). H(total) constante => min H_cond
        Hcond=sum((c/n)*math.log2(c) for c in grupos.values())  # menor = mejor
        # preferimos particion en muchos grupos parejos: usar #grupos y balance
        score=(len(grupos), -Hcond)
        if mejor is None or score>mejor[0]:
            mejor=(score, fam, grupos)
    return mejor  # None si nada discrimina

def discriminar(cands, respuestas=None, verbose=True):
    """Simula el arbol. respuestas: dict fam->valor para auto-responder (demo).
    Devuelve (#preguntas, candidato_final o lista_ambigua)."""
    q=0
    while len(cands)>1:
        m=mejor_pregunta(cands)
        if m is None:
            if verbose: print(f"  ! {len(cands)} candidatas indistinguibles por atributos conocidos")
            return q, [c[0] for c in cands]
        _,fam,grupos=m
        opciones=sorted(grupos)
        q+=1
        # elegir respuesta (demo) o preguntar
        if respuestas and fam in respuestas:
            val=respuestas[fam]
        else:
            val=opciones[0]  # demo: toma la primera
        if verbose:
            print(f"  P{q}: {fam}?  opciones={opciones}  -> resp: {val}  ({grupos[val]} quedan)")
        cands=[c for c in cands if c[2].get(fam,"—")==val]
    return q, (cands[0][0] if cands else None)

if __name__=="__main__":
    import sys
    F="tablota.csv"
    rows=list(csv.DictReader(open(F,encoding="utf-8-sig")))
    def candidatos(modelo, anio):
        out=[]
        for r in rows:
            if r["MODELO"].strip().upper()==modelo.upper() and r["AÑO"]==str(anio):
                out.append((r["CLAVE"], r["DESCRIPCION"], atributos(r["DESCRIPCION"], r["MODELO"])))
        return out

    casos=[("JETTA",2016),("MDX",2019),("CX-5",2020),("SENTRA",2019)]
    for modelo,anio in casos:
        cs=candidatos(modelo,anio)
        print(f"\n=== {modelo} {anio}: {len(cs)} candidatas ===")
        for k,d,a in cs:
            print(f"   {k} | {d}")
            print(f"        attrs={a}")
        if len(cs)>1:
            print("  --- discriminacion (demo, responde 1a opcion) ---")
            q,fin=discriminar([ (k,d,a) for k,d,a in cs])
            print(f"  => {q} pregunta(s), resultado: {fin}")
