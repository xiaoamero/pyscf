#!/usr/bin/env python
# Copyright 2014-2018 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#
# Ref:
# Chem Phys Lett, 256, 454
# J. Mol. Struct. THEOCHEM, 914, 3
#

from functools import reduce
import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf.ao2mo import _ao2mo
from pyscf.tdscf import rhf
from pyscf.pbc.dft import numint
from pyscf.pbc.scf.newton_ah import _gen_rhf_response
from pyscf import __config__

REAL_EIG_THRESHOLD = getattr(__config__, 'pbc_tdscf_rhf_TDDFT_pick_eig_threshold', 1e-3)
POSTIVE_EIG_THRESHOLD = getattr(__config__, 'pbc_tdscf_rhf_TDDFT_positive_eig_threshold', 1e-3)

class TDA(rhf.TDA):
#FIXME: numerically unstable with small mesh?
#TODO: Add a warning message for small mesh.

    conv_tol = getattr(__config__, 'pbc_tdscf_rhf_TDA_conv_tol', 1e-6)

    def __init__(self, mf):
        from pyscf.pbc import scf
        assert(isinstance(mf, scf.khf.KSCF))
        self.cell = mf.cell
        rhf.TDA.__init__(self, mf)
        from pyscf.pbc.df.df_ao2mo import warn_pbc2d_eri
        warn_pbc2d_eri(mf)

    def get_vind(self, mf):
        singlet = self.singlet
        cell = mf.cell
        kpts = mf.kpts

        mo_coeff = mf.mo_coeff
        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        nkpts = len(mo_occ)
        nao, nmo = mo_coeff[0].shape
        occidx = [numpy.where(mo_occ[k]==2)[0] for k in range(nkpts)]
        viridx = [numpy.where(mo_occ[k]==0)[0] for k in range(nkpts)]
        orbo = [mo_coeff[k][:,occidx[k]] for k in range(nkpts)]
        orbv = [mo_coeff[k][:,viridx[k]] for k in range(nkpts)]
        eai = _get_eai(mo_energy, mo_occ)
        hdiag = numpy.hstack([x.ravel() for x in eai])

        mem_now = lib.current_memory()[0]
        max_memory = max(2000, self.max_memory*.8-mem_now)
        vresp = _gen_rhf_response(mf, singlet, hermi=0, max_memory=max_memory)

        def vind(zs):
            nz = len(zs)
            z1s = [_unpack(z, mo_occ) for z in zs]
            dmvo = numpy.empty((nz,nkpts,nao,nao), dtype=numpy.complex128)
            for i in range(nz):
                # *2 for double occupancy
                dm1 = z1s[i] * 2
                for k in range(nkpts):
                    dmvo[i,k] = reduce(numpy.dot, (orbv[k], dm1[k], orbo[k].T.conj()))

            v1ao = vresp(dmvo)
            v1s = []
            for i in range(nz):
                dm1 = z1s[i]
                for k in range(nkpts):
                    v1vo = reduce(numpy.dot, (orbv[k].T.conj(), v1ao[i,k], orbo[k]))
                    v1vo += eai[k] * dm1[k]
                    v1s.append(v1vo.ravel())
            return lib.asarray(v1s).reshape(nz,-1)
        return vind, hdiag

    def init_guess(self, mf, nstates=None):
        if nstates is None: nstates = self.nstates

        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        eai = numpy.hstack([x.ravel() for x in _get_eai(mo_energy, mo_occ)])

        nov = eai.size
        nroot = min(nstates, nov)
        x0 = numpy.zeros((nroot, nov))
        idx = numpy.argsort(eai.ravel())
        for i in range(nroot):
            x0[i,idx[i]] = 1  # lowest excitations
        return x0

    def kernel(self, x0=None):
        '''TDA diagonalization solver
        '''
        self.check_sanity()
        self.dump_flags()

        vind, hdiag = self.get_vind(self._scf)
        precond = self.get_precond(hdiag)

        if x0 is None:
            x0 = self.init_guess(self._scf, self.nstates)
        self.converged, self.e, x1 = \
                lib.davidson1(vind, x0, precond,
                              tol=self.conv_tol,
                              nroots=self.nstates, lindep=self.lindep,
                              max_space=self.max_space,
                              verbose=self.verbose)

        mo_occ = self._scf.mo_occ
# 1/sqrt(2) because self.x is for alpha excitation amplitude and 2(X^+*X) = 1
        self.xy = [(_unpack(xi*numpy.sqrt(.5), mo_occ), 0) for xi in x1]
        return self.e, self.xy
CIS = TDA


class TDHF(TDA):
    def get_vind(self, mf):
        '''
        [ A   B ][X]
        [-B* -A*][Y]
        '''
        singlet = self.singlet
        cell = mf.cell
        kpts = mf.kpts

        mo_coeff = mf.mo_coeff
        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        nkpts = len(mo_occ)
        nao, nmo = mo_coeff[0].shape
        occidx = [numpy.where(mo_occ[k]==2)[0] for k in range(nkpts)]
        viridx = [numpy.where(mo_occ[k]==0)[0] for k in range(nkpts)]
        orbo = [mo_coeff[k][:,occidx[k]] for k in range(nkpts)]
        orbv = [mo_coeff[k][:,viridx[k]] for k in range(nkpts)]
        eai = _get_eai(mo_energy, mo_occ)
        hdiag = numpy.hstack([x.ravel() for x in eai])
        tot_x = hdiag.size
        hdiag = numpy.hstack((hdiag, hdiag))

        mem_now = lib.current_memory()[0]
        max_memory = max(2000, self.max_memory*.8-mem_now)
        vresp = _gen_rhf_response(mf, singlet, hermi=0, max_memory=max_memory)

        def vind(xys):
            nz = len(xys)
            z1xs = [_unpack(xy[:tot_x], mo_occ) for xy in xys]
            z1ys = [_unpack(xy[tot_x:], mo_occ) for xy in xys]
            dmvo = numpy.empty((nz,nkpts,nao,nao), dtype=numpy.complex128)
            for i in range(nz):
                # *2 for double occupancy
                dmx = z1xs[i] * 2
                dmy = z1ys[i] * 2
                for k in range(nkpts):
                    dmvo[i,k] = reduce(numpy.dot, (orbv[k], dmx[k], orbo[k].T.conj()))
                    dmvo[i,k]+= reduce(numpy.dot, (orbo[k], dmy[k].T, orbv[k].T.conj()))

            v1ao = vresp(dmvo)
            v1s = []
            for i in range(nz):
                dmx = z1xs[i]
                dmy = z1ys[i]
                v1xs = []
                v1ys = []
                for k in range(nkpts):
                    v1x = reduce(numpy.dot, (orbv[k].T.conj(), v1ao[i,k], orbo[k]))
                    v1y = reduce(numpy.dot, (orbo[k].T.conj(), v1ao[i,k], orbv[k])).T
                    v1x+= eai[k] * dmx[k]
                    v1y+= eai[k] * dmy[k]
                    v1xs.append(v1x.ravel())
                    v1ys.append(-v1y.ravel())
                v1s += v1xs + v1ys
            return lib.asarray(v1s).reshape(nz,-1)
        return vind, hdiag

    def init_guess(self, mf, nstates=None):
        x0 = TDA.init_guess(self, mf, nstates)
        y0 = numpy.zeros_like(x0)
        return numpy.hstack((x0,y0))

    def kernel(self, x0=None):
        '''TDHF diagonalization with non-Hermitian eigenvalue solver
        '''
        logger.warn(self, 'PBC-TDDFT is an experimental feature. '
                    'It is numerically sensitive to the accuracy of integrals '
                    '(relating to cell.precision).')

        self.check_sanity()
        self.dump_flags()

        vind, hdiag = self.get_vind(self._scf)
        precond = self.get_precond(hdiag)
        if x0 is None:
            x0 = self.init_guess(self._scf, self.nstates)

        # We only need positive eigenvalues
        def pickeig(w, v, nroots, envs):
            realidx = numpy.where((abs(w.imag) < REAL_EIG_THRESHOLD) &
                                  (w.real > POSTIVE_EIG_THRESHOLD))[0]
            return lib.linalg_helper._eigs_cmplx2real(w, v, realidx)

        self.converged, w, x1 = \
                lib.davidson_nosym1(vind, x0, precond,
                                    tol=self.conv_tol,
                                    nroots=self.nstates, lindep=self.lindep,
                                    max_space=self.max_space, pick=pickeig,
                                    verbose=self.verbose)
        mo_occ = self._scf.mo_occ
        self.e = w
        def norm_xy(z):
            x, y = z.reshape(2,-1)
            norm = 2*(lib.norm(x)**2 - lib.norm(y)**2)
            norm = 1/numpy.sqrt(norm)
            x *= norm
            y *= norm
            return _unpack(x, mo_occ), _unpack(y, mo_occ)
        self.xy = [norm_xy(z) for z in x1]

        return self.e, self.xy
RPA = TDHF


def _get_eai(mo_energy, mo_occ):
    eai = []
    for k, occ in enumerate(mo_occ):
        occidx = occ >  0
        viridx = occ == 0
        ai = lib.direct_sum('a-i->ai', mo_energy[k][viridx], mo_energy[k][occidx])
        eai.append(ai)
    return eai

def _unpack(vo, mo_occ):
    z = []
    ip = 0
    for k, occ in enumerate(mo_occ):
        no = numpy.count_nonzero(occ > 0)
        nv = occ.size - no
        z.append(vo[ip:ip+nv*no].reshape(nv,no))
        ip += nv * no
    return z


if __name__ == '__main__':
    from pyscf.pbc import gto
    from pyscf.pbc import scf
    from pyscf.pbc import df
    cell = gto.Cell()
    cell.unit = 'B'
    cell.atom = '''
    C  0.          0.          0.        
    C  1.68506879  1.68506879  1.68506879
    '''
    cell.a = '''
    0.          3.37013758  3.37013758
    3.37013758  0.          3.37013758
    3.37013758  3.37013758  0.
    '''

    cell.basis = 'gth-szv'
    cell.pseudo = 'gth-pade'
    cell.mesh = [25]*3
    cell.build()
    mf = scf.KRHF(cell, cell.make_kpts([2,1,1])).set(exxdiv=None)
    #mf.with_df = df.MDF(cell, cell.make_kpts([2,1,1]))
    #mf.with_df.auxbasis = 'weigend'
    #mf.with_df._cderi = 'eri3d-mdf.h5'
    #mf.with_df.build(with_j3c=False)
    mf.run()
#mesh=9  -8.65192427146353
#mesh=12 -8.65192352289817
#mesh=15 -8.6519235231529
#MDF mesh=5 -8.6519301815144

    td = TDA(mf)
    td.verbose = 5
    print(td.kernel()[0] * 27.2114)
#mesh=9  [ 6.0073749   6.09315355  6.3479901 ]
#mesh=12 [ 6.00253282  6.09317929  6.34799109]
#mesh=15 [ 6.00253396  6.09317949  6.34799109]
#MDF mesh=5 [ 6.09317489  6.09318265  6.34798637]

#    from pyscf.pbc import tools
#    scell = tools.super_cell(cell, [2,1,1])
#    mf = scf.RHF(scell).run()
#    td = rhf.TDA(mf)
#    td.verbose = 5
#    print(td.kernel()[0] * 27.2114)

    td = TDHF(mf)
    td.verbose = 5
    print(td.kernel()[0] * 27.2114)
#mesh=9  [ 6.03860914  6.21664545  8.20305225]
#mesh=12 [ 6.03868259  6.03860343  6.2167623 ]
#mesh=15 [ 6.03861321  6.03861324  6.21675868]
#MDF mesh=5 [ 6.03861693  6.03861775  6.21675694]

