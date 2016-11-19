#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
Selected CI

Simple usage::

    >>> from pyscf import gto, scf, ao2mo, fci
    >>> mol = gto.M(atom='C 0 0 0; C 0 0 1')
    >>> mf = scf.RHF(mol).run()
    >>> h1 = mf.mo_coeff.T.dot(mf.get_hcore()).dot(mf.mo_coeff)
    >>> h2 = ao2mo.kernel(mol, mf.mo_coeff)
    >>> e = fci.select_ci.kernel(h1, h2, mf.mo_coeff.shape[1], mol.nelectron)[0]
'''

import ctypes
import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf import ao2mo
from pyscf.fci import cistring
from pyscf.fci import direct_spin1
from pyscf.fci import rdm

libfci = lib.load_library('libfci')

def contract_2e(eri, civec_strs, norb, nelec, link_index=None):
    ci_coeff, nelec, ci_strs = _unpack(civec_strs, nelec)
    if link_index is None:
        link_index = _all_linkstr_index(ci_strs, norb, nelec)
    cd_indexa, dd_indexa, cd_indexb, dd_indexb = link_index
    na, nlinka = cd_indexa.shape[:2]
    nb, nlinkb = cd_indexb.shape[:2]
    ma, mlinka = dd_indexa.shape[:2]
    mb, mlinkb = dd_indexb.shape[:2]

    eri = ao2mo.restore(1, eri, norb)
    eri1 = eri.transpose(0,2,1,3) - eri.transpose(0,2,3,1)
    idx,idy = numpy.tril_indices(norb, -1)
    idx = idx * norb + idy
    eri1 = lib.take_2d(eri1.reshape(norb**2,-1), idx, idx) * 2
    fcivec = ci_coeff.reshape(na,nb)
    # (bb|bb)
    if nelec[1] > 1:
        fcivecT = lib.transpose(fcivec)
        ci1T = numpy.zeros((nb,na))
        libfci.SCIcontract_2e_aaaa(eri1.ctypes.data_as(ctypes.c_void_p),
                                   fcivecT.ctypes.data_as(ctypes.c_void_p),
                                   ci1T.ctypes.data_as(ctypes.c_void_p),
                                   ctypes.c_int(norb),
                                   ctypes.c_int(nb), ctypes.c_int(na),
                                   ctypes.c_int(mb), ctypes.c_int(mlinkb),
                                   dd_indexb.ctypes.data_as(ctypes.c_void_p))
        ci1 = lib.transpose(ci1T, out=fcivecT)
    else:
        ci1 = numpy.zeros_like(fcivec)
    # (aa|aa)
    if nelec[0] > 1:
        libfci.SCIcontract_2e_aaaa(eri1.ctypes.data_as(ctypes.c_void_p),
                                   fcivec.ctypes.data_as(ctypes.c_void_p),
                                   ci1.ctypes.data_as(ctypes.c_void_p),
                                   ctypes.c_int(norb),
                                   ctypes.c_int(na), ctypes.c_int(nb),
                                   ctypes.c_int(ma), ctypes.c_int(mlinka),
                                   dd_indexa.ctypes.data_as(ctypes.c_void_p))

    h_ps = numpy.einsum('pqqs->ps', eri)
    eri1 = eri * 2
    for k in range(norb):
        eri1[:,:,k,k] += h_ps/nelec[0]
        eri1[k,k,:,:] += h_ps/nelec[1]
    eri1 = ao2mo.restore(4, eri1, norb)
    # (bb|aa)
    libfci.SCIcontract_2e_bbaa(eri1.ctypes.data_as(ctypes.c_void_p),
                               fcivec.ctypes.data_as(ctypes.c_void_p),
                               ci1.ctypes.data_as(ctypes.c_void_p),
                               ctypes.c_int(norb),
                               ctypes.c_int(na), ctypes.c_int(nb),
                               ctypes.c_int(nlinka), ctypes.c_int(nlinkb),
                               cd_indexa.ctypes.data_as(ctypes.c_void_p),
                               cd_indexb.ctypes.data_as(ctypes.c_void_p))

    return _as_SCIvector(ci1.reshape(ci_coeff.shape), ci_strs)

def select_strs(myci, eri, eri_pq_max, civec_max, strs, norb, nelec):
    strs = numpy.asarray(strs, dtype=numpy.int64)
    nstrs = len(strs)
    nvir = norb - nelec
    strs_add = numpy.empty((nstrs*(nelec*nvir)**2//4), dtype=numpy.int64)
    libfci.SCIselect_strs.restype = ctypes.c_int
    nadd = libfci.SCIselect_strs(strs_add.ctypes.data_as(ctypes.c_void_p),
                                 strs.ctypes.data_as(ctypes.c_void_p),
                                 eri.ctypes.data_as(ctypes.c_void_p),
                                 eri_pq_max.ctypes.data_as(ctypes.c_void_p),
                                 civec_max.ctypes.data_as(ctypes.c_void_p),
                                 ctypes.c_double(myci.select_cutoff),
                                 ctypes.c_int(norb), ctypes.c_int(nelec),
                                 ctypes.c_int(nstrs))
    strs_add = sorted(set(strs_add[:nadd]) - set(strs))
    return numpy.asarray(strs_add, dtype=numpy.int64)

def enlarge_space(myci, civec_strs, eri, norb, nelec):
    if isinstance(civec_strs, (tuple, list)):
        nelec, (strsa, strsb) = _unpack(civec_strs[0], nelec)[1:]
        ci_coeff = lib.asarray(civec_strs)
    else:
        ci_coeff, nelec, (strsa, strsb) = _unpack(civec_strs, nelec)
    na = len(strsa)
    nb = len(strsb)
    ci0 = ci_coeff.reshape(-1,na,nb)
    abs_ci = abs(ci0).max(axis=0)
    civec_a_max = abs_ci.max(axis=1)
    civec_b_max = abs_ci.max(axis=0)
    ci_aidx = numpy.where(civec_a_max > myci.ci_coeff_cutoff)[0]
    ci_bidx = numpy.where(civec_b_max > myci.ci_coeff_cutoff)[0]
    civec_a_max = civec_a_max[ci_aidx]
    civec_b_max = civec_b_max[ci_bidx]
    strsa = strsa[ci_aidx]
    strsb = strsb[ci_bidx]

    eri = ao2mo.restore(1, eri, norb)
    eri_pq_max = abs(eri.reshape(norb**2,-1)).max(axis=1).reshape(norb,norb)

    strsa_add = select_strs(myci, eri, eri_pq_max, civec_a_max, strsa, norb, nelec[0])
    strsb_add = select_strs(myci, eri, eri_pq_max, civec_b_max, strsb, norb, nelec[1])
    strsa = numpy.append(strsa, strsa_add)
    strsb = numpy.append(strsb, strsb_add)
    aidx = numpy.argsort(strsa)
    bidx = numpy.argsort(strsb)
    ci_strs = (strsa[aidx], strsb[bidx])
    aidx = numpy.where(aidx < len(ci_aidx))[0]
    bidx = numpy.where(bidx < len(ci_bidx))[0]
    ma = len(strsa)
    mb = len(strsb)

    cs = []
    for i in range(ci0.shape[0]):
        ci1 = numpy.zeros((ma,mb))
        tmp = lib.take_2d(ci0[i], ci_aidx, ci_bidx)
        lib.takebak_2d(ci1, tmp, aidx, bidx)
        cs.append(_as_SCIvector(ci1, ci_strs))

    if ci_coeff[0].ndim == 0 or ci_coeff[0].shape[-1] != nb:
        cs = [c.ravel() for c in cs]

    if (isinstance(ci_coeff, numpy.ndarray) and
        ci_coeff.shape[0] == na or ci_coeff.shape[0] == na*nb):
        cs = cs[0]
    return cs

def cre_des_linkstr(strs, norb, nelec, tril=False):
    '''Given intermediates, the link table to generate input strs
    '''
    strs = numpy.asarray(strs, dtype=numpy.int64)
    nvir = norb - nelec
    nstrs = len(strs)
    link_index = numpy.zeros((nstrs,nelec+nelec*nvir,4), dtype=numpy.int32)
    libfci.SCIcre_des_linkstr(link_index.ctypes.data_as(ctypes.c_void_p),
                              ctypes.c_int(norb), ctypes.c_int(nstrs),
                              ctypes.c_int(nelec),
                              strs.ctypes.data_as(ctypes.c_void_p),
                              ctypes.c_int(tril))
    return link_index

def cre_des_linkstr_tril(strs, norb, nelec):
    '''Given intermediates, the link table to generate input strs
    '''
    return cre_des_linkstr(strs, norb, nelec, True)

def des_des_linkstr(strs, norb, nelec, tril=False):
    '''Given intermediates, the link table to generate input strs
    '''
    if nelec < 2:
        return None

    strs = numpy.asarray(strs, dtype=numpy.int64)
    nvir = norb - nelec
    nstrs = len(strs)
    inter1 = numpy.empty((nstrs*nelec), dtype=numpy.int64)
    libfci.SCIdes_uniq_strs.restype = ctypes.c_int
    ninter = libfci.SCIdes_uniq_strs(inter1.ctypes.data_as(ctypes.c_void_p),
                                     strs.ctypes.data_as(ctypes.c_void_p),
                                     ctypes.c_int(norb), ctypes.c_int(nelec),
                                     ctypes.c_int(nstrs))
    inter1 = numpy.asarray(sorted(set(inter1[:ninter])), dtype=numpy.int64)
    ninter = len(inter1)

    inter = numpy.empty((ninter*nelec), dtype=numpy.int64)
    ninter = libfci.SCIdes_uniq_strs(inter.ctypes.data_as(ctypes.c_void_p),
                                     inter1.ctypes.data_as(ctypes.c_void_p),
                                     ctypes.c_int(norb), ctypes.c_int(nelec-1),
                                     ctypes.c_int(ninter))
    inter = numpy.asarray(sorted(set(inter[:ninter])), dtype=numpy.int64)
    ninter = len(inter)

    nvir += 2
    link_index = numpy.zeros((ninter,nvir*nvir,4), dtype=numpy.int32)
    libfci.SCIdes_des_linkstr(link_index.ctypes.data_as(ctypes.c_void_p),
                              ctypes.c_int(norb), ctypes.c_int(nelec),
                              ctypes.c_int(nstrs), ctypes.c_int(ninter),
                              strs.ctypes.data_as(ctypes.c_void_p),
                              inter.ctypes.data_as(ctypes.c_void_p),
                              ctypes.c_int(tril))
    return link_index

def des_des_linkstr_tril(strs, norb, nelec):
    '''Given intermediates, the link table to generate input strs
    '''
    return des_des_linkstr(strs, norb, nelec, True)

def gen_des_linkstr(strs, norb, nelec):
    '''Given intermediates, the link table to generate input strs
    '''
    if nelec < 1:
        return None

    strs = numpy.asarray(strs, dtype=numpy.int64)
    nvir = norb - nelec
    nstrs = len(strs)
    inter = numpy.empty((nstrs*nelec), dtype=numpy.int64)
    libfci.SCIdes_uniq_strs.restype = ctypes.c_int
    ninter = libfci.SCIdes_uniq_strs(inter.ctypes.data_as(ctypes.c_void_p),
                                     strs.ctypes.data_as(ctypes.c_void_p),
                                     ctypes.c_int(norb), ctypes.c_int(nelec),
                                     ctypes.c_int(nstrs))
    inter = numpy.asarray(sorted(set(inter[:ninter])), dtype=numpy.int64)
    ninter = len(inter)

    nvir += 1
    link_index = numpy.zeros((ninter,nvir,4), dtype=numpy.int32)
    libfci.SCIdes_linkstr(link_index.ctypes.data_as(ctypes.c_void_p),
                          ctypes.c_int(norb), ctypes.c_int(nelec),
                          ctypes.c_int(nstrs), ctypes.c_int(ninter),
                          strs.ctypes.data_as(ctypes.c_void_p),
                          inter.ctypes.data_as(ctypes.c_void_p))
    return link_index

def gen_cre_linkstr(strs, norb, nelec):
    '''Given intermediates, the link table to generate input strs
    '''
    if nelec == norb:
        return None

    strs = numpy.asarray(strs, dtype=numpy.int64)
    nvir = norb - nelec
    nstrs = len(strs)
    inter = numpy.empty((nstrs*nvir), dtype=numpy.int64)
    libfci.SCIcre_uniq_strs.restype = ctypes.c_int
    ninter = libfci.SCIcre_uniq_strs(inter.ctypes.data_as(ctypes.c_void_p),
                                     strs.ctypes.data_as(ctypes.c_void_p),
                                     ctypes.c_int(norb), ctypes.c_int(nelec),
                                     ctypes.c_int(nstrs))
    inter = numpy.asarray(sorted(set(inter[:ninter])), dtype=numpy.int64)
    ninter = len(inter)

    link_index = numpy.zeros((ninter,nelec+1,4), dtype=numpy.int32)
    libfci.SCIcre_linkstr(link_index.ctypes.data_as(ctypes.c_void_p),
                          ctypes.c_int(norb), ctypes.c_int(nelec),
                          ctypes.c_int(nstrs), ctypes.c_int(ninter),
                          strs.ctypes.data_as(ctypes.c_void_p),
                          inter.ctypes.data_as(ctypes.c_void_p))
    return link_index


def make_hdiag(h1e, eri, ci_strs, norb, nelec):
    ci_coeff, nelec, ci_strs = _unpack(None, nelec, ci_strs)
    na = len(ci_strs[0])
    nb = len(ci_strs[1])
    hdiag = numpy.empty(na*nb)

    h1e = numpy.asarray(h1e, order='C')
    eri = ao2mo.restore(1, eri, norb)
    jdiag = numpy.asarray(numpy.einsum('iijj->ij',eri), order='C')
    kdiag = numpy.asarray(numpy.einsum('ijji->ij',eri), order='C')
    c_h1e = h1e.ctypes.data_as(ctypes.c_void_p)
    c_jdiag = jdiag.ctypes.data_as(ctypes.c_void_p)
    c_kdiag = kdiag.ctypes.data_as(ctypes.c_void_p)
    libfci.FCImake_hdiag_uhf(hdiag.ctypes.data_as(ctypes.c_void_p),
                             c_h1e, c_h1e, c_jdiag, c_jdiag, c_jdiag, c_kdiag, c_kdiag,
                             ctypes.c_int(norb),
                             ctypes.c_int(na), ctypes.c_int(nb),
                             ctypes.c_int(nelec[0]), ctypes.c_int(nelec[1]),
                             ci_strs[0].ctypes.data_as(ctypes.c_void_p),
                             ci_strs[1].ctypes.data_as(ctypes.c_void_p))
    return hdiag

def kernel_fixed_space(myci, h1e, eri, norb, nelec, ci_strs, ci0=None,
                       tol=None, lindep=None, max_cycle=None, max_space=None,
                       nroots=None, davidson_only=None,
                       max_memory=None, verbose=None, ecore=0, **kwargs):
    if verbose is None:
        log = logger.Logger(myci.stdout, myci.verbose)
    elif isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(myci.stdout, verbose)
    if tol is None: tol = myci.conv_tol
    if lindep is None: lindep = myci.lindep
    if max_cycle is None: max_cycle = myci.max_cycle
    if max_space is None: max_space = myci.max_space
    if max_memory is None: max_memory = myci.max_memory
    if nroots is None: nroots = myci.nroots
    if myci.verbose >= logger.WARN:
        myci.check_sanity()

    if myci.spin is not None:
        if isinstance(nelec, (int, numpy.number)):
            nelec = (nelec+myci.spin)//2, (nelec-myci.spin)//2
        else:
            nelec = (sum(nelec)+myci.spin)//2, (sum(nelec)-myci.spin)//2

    ci0, nelec, ci_strs = _unpack(ci0, nelec, ci_strs)
    na = len(ci_strs[0])
    nb = len(ci_strs[1])
    h2e = direct_spin1.absorb_h1e(h1e, eri, norb, nelec, .5)
    h2e = ao2mo.restore(1, h2e, norb)

    link_index = _all_linkstr_index(ci_strs, norb, nelec)
    hdiag = myci.make_hdiag(h1e, eri, ci_strs, norb, nelec)

    if isinstance(ci0, _SCIvector):
        if ci0.size == na*nb:
            ci0 = [ci0.ravel()]
        else:
            ci0 = [x.ravel() for x in ci0]
    else:
        ci0 = myci.get_init_guess(ci_strs, norb, nelec, nroots, hdiag)

    def hop(c):
        hc = myci.contract_2e(h2e, _as_SCIvector(c, ci_strs), norb, nelec, link_index)
        return hc.reshape(-1)
    precond = lambda x, e, *args: x/(hdiag-e+1e-4)

    #e, c = lib.davidson(hop, ci0, precond, tol=myci.conv_tol)
    e, c = myci.eig(hop, ci0, precond, tol=tol, lindep=lindep,
                    max_cycle=max_cycle, max_space=max_space, nroots=nroots,
                    max_memory=max_memory, verbose=log, **kwargs)
    if nroots > 1:
        return e+ecore, [_as_SCIvector(ci.reshape(na,nb),ci_strs) for ci in c]
    else:
        return e+ecore, _as_SCIvector(c.reshape(na,nb), ci_strs)


def kernel_float_space(myci, h1e, eri, norb, nelec, ci0=None,
                       tol=None, lindep=None, max_cycle=None, max_space=None,
                       nroots=None, davidson_only=None,
                       max_memory=None, verbose=None, ecore=0, **kwargs):
    if verbose is None:
        log = logger.Logger(myci.stdout, myci.verbose)
    elif isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(myci.stdout, verbose)
    if tol is None: tol = myci.conv_tol
    if lindep is None: lindep = myci.lindep
    if max_cycle is None: max_cycle = myci.max_cycle
    if max_space is None: max_space = myci.max_space
    if max_memory is None: max_memory = myci.max_memory
    if nroots is None: nroots = myci.nroots
    if myci.verbose >= logger.WARN:
        myci.check_sanity()

    if myci.spin is not None:
        if isinstance(nelec, (int, numpy.number)):
            nelec = (nelec+myci.spin)//2, (nelec-myci.spin)//2
        else:
            nelec = (sum(nelec)+myci.spin)//2, (sum(nelec)-myci.spin)//2

    h2e = direct_spin1.absorb_h1e(h1e, eri, norb, nelec, .5)
    h2e = ao2mo.restore(1, h2e, norb)

# TODO: initial guess from CISD
    if isinstance(ci0, _SCIvector):
        if ci0.size == len(ci0._strs[0])*len(ci0._strs[1]):
            ci0 = [ci0.ravel()]
        else:
            ci0 = [x.ravel() for x in ci0]
    else:
        if isinstance(nelec, (int, numpy.integer)):
            nelecb = nelec//2
            neleca = nelec - nelecb
            nelec = neleca, nelecb
        ci_strs = (numpy.asarray([int('1'*nelec[0], 2)]),
                   numpy.asarray([int('1'*nelec[1], 2)]))
        ci0 = _as_SCIvector(numpy.ones((1,1)), ci_strs)
        ci0 = myci.enlarge_space(ci0, h2e, norb, nelec)
        ci_strs = ci0._strs
        hdiag = myci.make_hdiag(h1e, eri, ci_strs, norb, nelec)
        ci0 = myci.get_init_guess(ci_strs, norb, nelec, nroots, hdiag)

    def hop(c):
        hc = myci.contract_2e(h2e, _as_SCIvector(c, ci_strs), norb, nelec, link_index)
        return hc.ravel()
    precond = lambda x, e, *args: x/(hdiag-e+myci.level_shift)

    namax = cistring.num_strings(norb, nelec[0])
    nbmax = cistring.num_strings(norb, nelec[1])
    e_last = 0
    float_tol = 3e-4
    conv = False
    for icycle in range(norb):
        ci_strs = ci0[0]._strs
        float_tol = max(float_tol*.3, tol*1e2)
        log.debug('cycle %d  ci.shape %s  float_tol %g',
                  icycle, (len(ci_strs[0]), len(ci_strs[1])), float_tol)

        link_index = _all_linkstr_index(ci_strs, norb, nelec)
        hdiag = myci.make_hdiag(h1e, eri, ci_strs, norb, nelec)
        #e, ci0 = lib.davidson(hop, ci0.reshape(-1), precond, tol=float_tol)
        e, ci0 = myci.eig(hop, ci0, precond, tol=float_tol, lindep=lindep,
                          max_cycle=max_cycle, max_space=max_space, nroots=nroots,
                          max_memory=max_memory, verbose=log, **kwargs)
        if nroots > 1:
            ci0 = [_as_SCIvector(c, ci_strs) for c in ci0]
            de, e_last = min(e)-e_last, min(e)
            log.info('cycle %d  E = %s  dE = %.8g', icycle, e+ecore, de)
        else:
            ci0 = [_as_SCIvector(ci0, ci_strs)]
            de, e_last = e-e_last, e
            log.info('cycle %d  E = %.15g  dE = %.8g', icycle, e+ecore, de)

        if ci0[0].shape == (namax,nbmax) or abs(de) < tol*1e3:
            conv = True
            break

        last_ci0_size = float(len(ci_strs[0])), float(len(ci_strs[1]))
        ci0 = myci.enlarge_space(ci0, h2e, norb, nelec)
        na = len(ci0[0]._strs[0])
        nb = len(ci0[0]._strs[1])
        if ((.99 < na/last_ci0_size[0] < 1.01) and
            (.99 < nb/last_ci0_size[1] < 1.01)):
            conv = True
            break

    ci_strs = ci0[0]._strs
    log.debug('Extra CI in selected space %s', (len(ci_strs[0]), len(ci_strs[1])))
    link_index = _all_linkstr_index(ci_strs, norb, nelec)
    hdiag = myci.make_hdiag(h1e, eri, ci_strs, norb, nelec)
    e, c = myci.eig(hop, ci0, precond, tol=tol, lindep=lindep,
                    max_cycle=max_cycle, max_space=max_space, nroots=nroots,
                    max_memory=max_memory, verbose=log, **kwargs)
    log.info('Select CI  E = %.15g', e+ecore)

    na = len(ci_strs[0])
    nb = len(ci_strs[1])
    if nroots > 1:
        return e+ecore, [_as_SCIvector(ci.reshape(na,nb),ci_strs) for ci in c]
    else:
        return e+ecore, _as_SCIvector(c.reshape(na,nb), ci_strs)

def kernel(h1e, eri, norb, nelec, ci0=None, level_shift=1e-3, tol=1e-10,
           lindep=1e-14, max_cycle=50, max_space=12, nroots=1,
           davidson_only=False, pspace_size=400, orbsym=None, wfnsym=None,
           select_cutoff=1e-3, ci_coeff_cutoff=1e-3, ecore=0, **kwargs):
    return direct_spin1._kfactory(SelectCI, h1e, eri, norb, nelec, ci0,
                                  level_shift, tol, lindep, max_cycle,
                                  max_space, nroots, davidson_only,
                                  pspace_size, select_cutoff=select_cutoff,
                                  ci_coeff_cutoff=ci_coeff_cutoff, ecore=ecore,
                                  **kwargs)

# dm_pq = <|p^+ q|>
def make_rdm1s(civec_strs, norb, nelec, link_index=None):
    '''Spin searated 1-particle density matrices, (alpha,beta)
    '''
    ci_coeff, nelec, ci_strs = _unpack(civec_strs, nelec)
    if link_index is None:
        cd_indexa = cre_des_linkstr(ci_strs[0], norb, nelec[0])
        cd_indexb = cre_des_linkstr(ci_strs[1], norb, nelec[1])
    else:
        cd_indexa, dd_indexa, cd_indexb, dd_indexb = link_index
    rdm1a = rdm.make_rdm1_spin1('FCImake_rdm1a', ci_coeff, ci_coeff,
                                norb, nelec, (cd_indexa,cd_indexb))
    rdm1b = rdm.make_rdm1_spin1('FCImake_rdm1b', ci_coeff, ci_coeff,
                                norb, nelec, (cd_indexa,cd_indexb))
    return rdm1a, rdm1b

# spacial part of DM, dm_pq = <|p^+ q|>
def make_rdm1(civec_strs, norb, nelec, link_index=None):
    '''spin-traced 1-particle density matrix
    '''
    rdm1a, rdm1b = make_rdm1s(civec_strs, norb, nelec, link_index)
    return rdm1a + rdm1b

# dm_pq,rs = <|p^+ q r^+ s|>
def make_rdm2s(civec_strs, norb, nelec, link_index=None, **kwargs):
    ci_coeff, nelec, ci_strs = _unpack(civec_strs, nelec)
    if link_index is None:
        cd_indexa = cre_des_linkstr(ci_strs[0], norb, nelec[0])
        dd_indexa = des_des_linkstr(ci_strs[0], norb, nelec[0])
        cd_indexb = cre_des_linkstr(ci_strs[1], norb, nelec[1])
        dd_indexb = des_des_linkstr(ci_strs[1], norb, nelec[1])
    else:
        cd_indexa, dd_indexa, cd_indexb, dd_indexb = link_index
    na, nlinka = cd_indexa.shape[:2]
    nb, nlinkb = cd_indexb.shape[:2]
    ma, mlinka = dd_indexa.shape[:2]
    mb, mlinkb = dd_indexb.shape[:2]

    fcivec = ci_coeff.reshape(na,nb)
    # (bb|aa) and (aa|bb)
    dm2ab = rdm.make_rdm12_spin1('FCItdm12kern_ab', fcivec, fcivec,
                                 norb, nelec, (cd_indexa,cd_indexb), 0)[1]
    # (aa|aa)
    if nelec[0] > 1:
        dm2aa = numpy.empty([norb]*4)
        libfci.SCIrdm2_aaaa(libfci.SCIrdm2kern_aaaa,
                            dm2aa.ctypes.data_as(ctypes.c_void_p),
                            fcivec.ctypes.data_as(ctypes.c_void_p),
                            fcivec.ctypes.data_as(ctypes.c_void_p),
                            ctypes.c_int(norb),
                            ctypes.c_int(na), ctypes.c_int(nb),
                            ctypes.c_int(ma), ctypes.c_int(mlinka),
                            dd_indexa.ctypes.data_as(ctypes.c_void_p))
    # (bb|bb)
    if nelec[1] > 1:
        dm2bb = numpy.empty([norb]*4)
        fcivecT = lib.transpose(fcivec)
        libfci.SCIrdm2_aaaa(libfci.SCIrdm2kern_aaaa,
                            dm2bb.ctypes.data_as(ctypes.c_void_p),
                            fcivecT.ctypes.data_as(ctypes.c_void_p),
                            fcivecT.ctypes.data_as(ctypes.c_void_p),
                            ctypes.c_int(norb),
                            ctypes.c_int(nb), ctypes.c_int(na),
                            ctypes.c_int(mb), ctypes.c_int(mlinkb),
                            dd_indexb.ctypes.data_as(ctypes.c_void_p))
    return dm2aa, dm2ab, dm2bb

def make_rdm2(civec_strs, norb, nelec, link_index=None, **kwargs):
    r'''Spin traced 2-particle density matrice

    NOTE the 2pdm is :math:`\langle p^\dagger q^\dagger s r\rangle` but
    stored as [p,r,q,s]
    '''
    dm2aa, dm2ab, dm2bb = make_rdm2s(civec_strs, norb, nelec, link_index)
    dm2aa += dm2bb
    dm2aa += dm2ab
    dm2aa += dm2ab.transpose(2,3,0,1)
    return dm2aa

def trans_rdm1s(cibra_strs, ciket_strs, norb, nelec, link_index=None):
    '''Spin separated transition 1-particle density matrices
    '''
    cibra, nelec, ci_strs = _unpack(cibra_strs, nelec)
    ciket, nelec1, ci_strs1 = _unpack(ciket_strs, nelec)
    assert(all(ci_strs[0] == ci_strs1[0]) and
           all(ci_strs[1] == ci_strs1[1]))
    if link_index is None:
        cd_indexa = cre_des_linkstr(ci_strs[0], norb, nelec[0])
        cd_indexb = cre_des_linkstr(ci_strs[1], norb, nelec[1])
    else:
        cd_indexa, dd_indexa, cd_indexb, dd_indexb = link_index
    rdm1a = rdm.make_rdm1_spin1('FCItrans_rdm1a', cibra, ciket,
                                norb, nelec, (cd_indexa,cd_indexb))
    rdm1b = rdm.make_rdm1_spin1('FCItrans_rdm1b', cibra, ciket,
                                norb, nelec, (cd_indexa,cd_indexb))
    return rdm1a, rdm1b

# spacial part of DM
def trans_rdm1(cibra_strs, ciket_strs, norb, nelec, link_index=None):
    '''Spin traced transition 1-particle density matrices
    '''
    rdm1a, rdm1b = trans_rdm1s(cibra_strs, ciket_strs, norb, nelec, link_index)
    return rdm1a + rdm1b

def spin_square(civec_strs, norb, nelec):
    '''Spin square for RHF-FCI CI wfn only (obtained from spin-degenerated
    Hamiltonian)'''
    ci1 = contract_ss(civec_strs, norb, nelec)

    ss = numpy.einsum('ij,ij->', civec_strs.reshape(ci1.shape), ci1)
    s = numpy.sqrt(ss+.25) - .5
    multip = s*2+1
    return ss, multip

def contract_ss(civec_strs, norb, nelec):
    r''' S^2 |\Psi\rangle
    '''
    ci_coeff, nelec, ci_strs = _unpack(civec_strs, nelec)
    strsa, strsb = ci_strs
    neleca, nelecb = nelec
    ci_coeff = ci_coeff.reshape(len(strsa),len(strsb))

    def gen_map(fstr_index, strs, nelec, des=True):
        a_index = fstr_index(strs, norb, nelec)
        amap = numpy.zeros((a_index.shape[0],norb,2), dtype=numpy.int32)
        if des:
            for k, tab in enumerate(a_index):
                sign = tab[:,3]
                tab = tab[sign!=0]
                amap[k,tab[:,1]] = tab[:,2:]
        else:
            for k, tab in enumerate(a_index):
                sign = tab[:,3]
                tab = tab[sign!=0]
                amap[k,tab[:,0]] = tab[:,2:]
        return amap

    if neleca > 0:
        ades = gen_map(gen_des_linkstr, strsa, neleca)
    else:
        ades = None

    if nelecb > 0:
        bdes = gen_map(gen_des_linkstr, strsb, nelecb)
    else:
        bdes = None

    if neleca < norb:
        acre = gen_map(gen_cre_linkstr, strsa, neleca, False)
    else:
        acre = None

    if nelecb < norb:
        bcre = gen_map(gen_cre_linkstr, strsb, nelecb, False)
    else:
        bcre = None

    def trans(ci1, aindex, bindex):
        if aindex is None or bindex is None:
            return None

        ma = len(aindex)
        mb = len(bindex)
        t1 = numpy.zeros((ma,mb))
        for i in range(norb):
            signa = aindex[:,i,1]
            signb = bindex[:,i,1]
            maska = numpy.where(signa!=0)[0]
            maskb = numpy.where(signb!=0)[0]
            addra = aindex[maska,i,0]
            addrb = bindex[maskb,i,0]
            citmp = lib.take_2d(ci_coeff, addra, addrb)
            citmp *= signa[maska].reshape(-1,1)
            citmp *= signb[maskb]
            #: t1[addra.reshape(-1,1),addrb] += citmp
            lib.takebak_2d(t1, citmp, maska, maskb)
        for i in range(norb):
            signa = aindex[:,i,1]
            signb = bindex[:,i,1]
            maska = numpy.where(signa!=0)[0]
            maskb = numpy.where(signb!=0)[0]
            addra = aindex[maska,i,0]
            addrb = bindex[maskb,i,0]
            citmp = lib.take_2d(t1, maska, maskb)
            citmp *= signa[maska].reshape(-1,1)
            citmp *= signb[maskb]
            #: ci1[maska.reshape(-1,1), maskb] += citmp
            lib.takebak_2d(ci1, citmp, addra, addrb)

    ci1 = numpy.zeros_like(ci_coeff)
    trans(ci1, ades, bcre) # S+*S-
    trans(ci1, acre, bdes) # S-*S+
    ci1 *= .5
    ci1 += (neleca-nelecb)**2*.25*ci_coeff
    return _as_SCIvector(ci1, ci_strs)

def to_fci(civec_strs, norb, nelec):
    ci_coeff, nelec, ci_strs = _unpack(civec_strs, nelec)
    addrsa = [cistring.str2addr(norb, nelec[0], x) for x in ci_strs[0]]
    addrsb = [cistring.str2addr(norb, nelec[1], x) for x in ci_strs[1]]
    na = cistring.num_strings(norb, nelec[0])
    nb = cistring.num_strings(norb, nelec[1])
    ci0 = numpy.zeros((na,nb))
    lib.takebak_2d(ci0, ci_coeff, addrsa, addrsb)
    return ci0

def from_fci(fcivec, ci_strs, norb, nelec):
    fcivec, nelec, ci_strs = _unpack(fcivec, nelec, ci_strs)
    addrsa = [cistring.str2addr(norb, nelec[0], x) for x in ci_strs[0]]
    addrsb = [cistring.str2addr(norb, nelec[1], x) for x in ci_strs[1]]
    na = cistring.num_strings(norb, nelec[0])
    nb = cistring.num_strings(norb, nelec[1])
    fcivec = fcivec.reshape(na,nb)
    civec = lib.take_2d(fcivec, addrsa, addrsb)
    return _as_SCIvector(civec, ci_strs)


class SelectCI(direct_spin1.FCISolver):
    def __init__(self, mol=None):
        direct_spin1.FCISolver.__init__(self, mol)
        self.ci_coeff_cutoff = .5e-3
        self.select_cutoff = .5e-3
        self.conv_tol = 1e-9

##################################################
# don't modify the following attributes, they are not input options
        #self.converged = False
        #self.ci = None
        self._strs = None
        self._keys = set(self.__dict__.keys())

    def dump_flags(self, verbose=None):
        direct_spin1.FCISolver.dump_flags(self, verbose)
        logger.info(self, 'ci_coeff_cutoff %g', self.ci_coeff_cutoff)
        logger.info(self, 'select_cutoff   %g', self.select_cutoff)

    def contract_2e(self, eri, civec_strs, norb, nelec, link_index=None, **kwargs):
# The argument civec_strs is a CI vector in function FCISolver.contract_2e.
# Save and patch self._strs to make this contract_2e function compatible to
# FCISolver.contract_2e.
        if hasattr(civec_strs, '_strs'):
            self._strs = civec_strs._strs
        else:
            assert(civec_strs.size == len(self._strs[0])*len(self._strs[1]))
            civec_strs = _as_SCIvector(civec_strs, self._strs)
        return contract_2e(eri, civec_strs, norb, nelec, link_index)

    def get_init_guess(self, ci_strs, norb, nelec, nroots, hdiag):
        '''Initial guess is the single Slater determinant
        '''
        na = len(ci_strs[0])
        nb = len(ci_strs[1])
        ci0 = direct_spin1._get_init_guess(na, nb, nroots, hdiag)
        return [_as_SCIvector(x, ci_strs) for x in ci0]

    def make_hdiag(self, h1e, eri, ci_strs, norb, nelec):
        return make_hdiag(h1e, eri, ci_strs, norb, nelec)

    enlarge_space = enlarge_space
    kernel = kernel_float_space
    kernel_fixed_space = kernel_fixed_space

#    def approx_kernel(self, h1e, eri, norb, nelec, ci0=None, link_index=None,
#                      tol=None, lindep=None, max_cycle=None,
#                      max_memory=None, verbose=None, **kwargs):
#        ci_strs = getattr(ci0, '_strs', self._strs)
#        return self.kernel_fixed_space(h1e, eri, norb, nelec, ci_strs,
#                                       ci0, link_index, tol, lindep, 6,
#                                       max_memory, verbose, **kwargs)

    @lib.with_doc(spin_square.__doc__)
    def spin_square(self, civec_strs, norb, nelec):
        if isinstance(civec_strs, numpy.ndarray):
            return spin_square(_as_SCIvector_if_not(civec_strs, self._strs), norb, nelec)
        else:
            ss = [spin_square(_as_SCIvector_if_not(c, self._strs), norb, nelec)
                  for c in civec_strs]
            return [x[0] for x in ss], [x[1] for x in ss]

    def large_ci(self, civec_strs, norb, nelec, tol=.1):
        def check(x):
            ci, _, (strsa, strsb) = _unpack(x, nelec, self._strs)
            return [(ci[i,j], bin(strsa[i]), bin(strsa[j]))
                    for i,j in numpy.argwhere(abs(ci) > tol)]
        if isinstance(civec_strs, numpy.ndarray):
            return check(civec_strs)
        else:
            return [check(x) for x in civec_strs]

    def contract_ss(self, fcivec, norb, nelec):
        return contract_ss(fcivec, norb, nelec)

    @lib.with_doc(make_rdm1s.__doc__)
    def make_rdm1s(self, civec_strs, norb, nelec, link_index=None):
        civec_strs = _as_SCIvector_if_not(civec_strs, self._strs)
        return make_rdm1s(civec_strs, norb, nelec, link_index)

    @lib.with_doc(make_rdm1.__doc__)
    def make_rdm1(self, civec_strs, norb, nelec, link_index=None):
        rdm1a, rdm1b = self.make_rdm1s(civec_strs, norb, nelec, link_index)
        return rdm1a + rdm1b

    @lib.with_doc(make_rdm2s.__doc__)
    def make_rdm2s(self, civec_strs, norb, nelec, link_index=None, **kwargs):
        civec_strs = _as_SCIvector_if_not(civec_strs, self._strs)
        return make_rdm2s(civec_strs, norb, nelec, link_index)

    @lib.with_doc(make_rdm2.__doc__)
    def make_rdm2(self, civec_strs, norb, nelec, link_index=None, **kwargs):
        civec_strs = _as_SCIvector_if_not(civec_strs, self._strs)
        return make_rdm2(civec_strs, norb, nelec, link_index)

    def make_rdm12s(self, civec_strs, norb, nelec, link_index=None, **kwargs):
        civec_strs = _as_SCIvector_if_not(civec_strs, self._strs)
        if isinstance(nelec, (int, numpy.integer)):
            nelecb = nelec//2
            neleca = nelec - nelecb
        else:
            neleca, nelecb = nelec
        dm2aa, dm2ab, dm2bb = make_rdm2s(civec_strs, norb, nelec, link_index)
        if neleca > 1 and nelecb > 1:
            dm1a = numpy.einsum('iikl->kl', dm2aa) / (neleca-1)
            dm1b = numpy.einsum('iikl->kl', dm2bb) / (nelecb-1)
        else:
            dm1a, dm1b = make_rdm1s(civec_strs, norb, nelec, link_index)
        return (dm1a, dm1b), (dm2aa, dm2ab, dm2bb)

    def make_rdm12(self, civec_strs, norb, nelec, link_index=None, **kwargs):
        civec_strs = _as_SCIvector_if_not(civec_strs, self._strs)
        if isinstance(nelec, (int, numpy.integer)):
            nelec_tot = nelec
        else:
            nelec_tot = sum(nelec)
        dm2 = make_rdm2(civec_strs, norb, nelec, link_index)
        if nelec_tot > 1:
            dm1 = numpy.einsum('iikl->kl', dm2) / (nelec_tot-1)
        else:
            dm1 = make_rdm1(civec_strs, norb, nelec, link_index)
        return dm1, dm2

    @lib.with_doc(trans_rdm1s.__doc__)
    def trans_rdm1s(self, cibra, ciket, norb, nelec, link_index=None):
        cibra = _as_SCIvector_if_not(cibra, self._strs)
        ciket = _as_SCIvector_if_not(ciket, self._strs)
        return trans_rdm1s(cibra, ciket, norb, nelec, link_index)

    @lib.with_doc(trans_rdm1.__doc__)
    def trans_rdm1(self, cibra, ciket, norb, nelec, link_index=None):
        cibra = _as_SCIvector_if_not(cibra, self._strs)
        ciket = _as_SCIvector_if_not(ciket, self._strs)
        return trans_rdm1(cibra, ciket, norb, nelec, link_index)

SCI = SelectCI


def _unpack(civec_strs, nelec, ci_strs=None):
    if isinstance(nelec, (int, numpy.integer)):
        nelecb = nelec//2
        neleca = nelec - nelecb
    else:
        neleca, nelecb = nelec
    ci_strs = getattr(civec_strs, '_strs', ci_strs)
    if ci_strs is not None:
        strsa, strsb = ci_strs
        strsa = numpy.asarray(strsa)
        strsb = numpy.asarray(strsb)
        ci_strs = (strsa, strsb)
    return civec_strs, (neleca, nelecb), ci_strs

def _all_linkstr_index(ci_strs, norb, nelec):
    cd_indexa = cre_des_linkstr_tril(ci_strs[0], norb, nelec[0])
    dd_indexa = des_des_linkstr_tril(ci_strs[0], norb, nelec[0])
    cd_indexb = cre_des_linkstr_tril(ci_strs[1], norb, nelec[1])
    dd_indexb = des_des_linkstr_tril(ci_strs[1], norb, nelec[1])
    return cd_indexa, dd_indexa, cd_indexb, dd_indexb

# numpy.ndarray does not allow to attach attribtues.  Overwrite the
# numpy.ndarray class to tag the ._strs attribute
class _SCIvector(numpy.ndarray):
    def __array_finalize__(self, obj):
        self._strs = getattr(obj, '_strs', None)
def _as_SCIvector(civec, ci_strs):
    civec = civec.view(_SCIvector)
    civec._strs = ci_strs
    return civec
def _as_SCIvector_if_not(civec, ci_strs):
    if not hasattr(civec, '_strs'):
        civec = _as_SCIvector(civec, ci_strs)
    return civec

if __name__ == '__main__':
    from functools import reduce
    from pyscf import gto
    from pyscf import scf
    from pyscf import ao2mo
    from pyscf.fci import spin_op
    from pyscf.fci import addons

    mol = gto.Mole()
    mol.verbose = 0
    mol.output = None
    mol.atom = [
        ['H', ( 1.,-1.    , 0.   )],
        ['H', ( 0.,-1.    ,-1.   )],
        ['H', ( 1.,-0.5   ,-1.   )],
        ['H', ( 0.,-0.    ,-1.   )],
        ['H', ( 1.,-0.5   , 0.   )],
        ['H', ( 0., 1.    , 1.   )],
        ['H', ( 1., 2.    , 3.   )],
        ['H', ( 1., 2.    , 4.   )],
    ]
    mol.basis = 'sto-3g'
    mol.build()

    m = scf.RHF(mol)
    m.kernel()
    norb = m.mo_coeff.shape[1]
    nelec = mol.nelectron
    h1e = reduce(numpy.dot, (m.mo_coeff.T, m.get_hcore(), m.mo_coeff))
    eri = ao2mo.kernel(m._eri, m.mo_coeff, compact=False)
    eri = eri.reshape(norb,norb,norb,norb)

    e1, c1 = kernel(h1e, eri, norb, nelec)
    e2, c2 = direct_spin1.kernel(h1e, eri, norb, nelec)
    print(e1, e1 - -11.894559902235565, 'diff to FCI', e1-e2)

    print(c1.shape, c2.shape)
    dm1_1 = make_rdm1(c1, norb, nelec)
    dm1_2 = direct_spin1.make_rdm1(c2, norb, nelec)
    print(abs(dm1_1 - dm1_2).sum())
    dm2_1 = make_rdm2(c1, norb, nelec)
    dm2_2 = direct_spin1.make_rdm12(c2, norb, nelec)[1]
    print(abs(dm2_1 - dm2_2).sum())

    myci = SelectCI()
    e, c = kernel_fixed_space(myci, h1e, eri, norb, nelec, c1._strs)
    print(e - -11.894559902235565)

    print(myci.large_ci(c1, norb, nelec))
    print(myci.spin_square(c1, norb, nelec)[0] -
          spin_op.spin_square0(to_fci(c1, norb, nelec), norb, nelec)[0])

    myci = SelectCI()
    myci = addons.fix_spin_(myci)
    e1, c1 = myci.kernel(h1e, eri, norb, nelec)
    print(e1, e1 - -11.89467612053687)
    print(myci.spin_square(c1, norb, nelec))
